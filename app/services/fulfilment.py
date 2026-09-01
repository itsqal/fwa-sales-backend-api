"""Shipment, goods receipt, stock, and the allocation that closes the seam.

The last service in the chain. Everything before it moves stock between three companies
with no effect on the mobile app; :func:`allocate` sets ``fwa_inventory.allocated_ae_id``
and a unit becomes visible to a salesman's barcode scanner.

That column has exactly one writer, and this is it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationFailedError
from app.core.msisdn import normalise_msisdn
from app.core.pagination import Pagination
from app.models.admin import AdminUser
from app.models.enums import (
    AdminRole,
    AllocationMode,
    DevicePoStatus,
    InventoryStatus,
    ShipmentMilestoneType,
)
from app.models.fulfilment import (
    GoodsReceipt,
    Shipment,
    ShipmentMilestone,
    StockAllocation,
    StockAllocationItem,
)
from app.models.identity import AccountExecutive
from app.models.inventory import DeviceModel, FwaInventory
from app.models.purchasing import DevicePo, DevicePoStatusHistory
from app.schemas.fulfilment import (
    AllocationDetailOut,
    AllocationOut,
    CreateAllocationRequest,
    CreateShipmentRequest,
    MilestoneOut,
    MilestoneRequest,
    ReceiptOut,
    ShipmentOut,
    StockBundleOut,
    StockLineOut,
)
from app.services.device_po import _get_scoped as _get_po_scoped
from app.services.supply_common import ensure_transition

# The order the Delivery Progress tracker draws them in.
MILESTONE_ORDER = [
    ShipmentMilestoneType.SHIPPED,
    ShipmentMilestoneType.IN_TRANSIT,
    ShipmentMilestoneType.OUT_FOR_DELIVERY,
    ShipmentMilestoneType.DELIVERED,
]


def _require_dp(admin: AdminUser) -> None:
    if admin.role is not AdminRole.DP_ADMIN:
        raise ForbiddenError(
            "Only the supplying Device Partner can do this.", code="ROLE_NOT_PERMITTED"
        )


def _require_mpx(admin: AdminUser) -> uuid.UUID:
    if admin.role is not AdminRole.MPX_ADMIN or admin.mpx_id is None:
        raise ForbiddenError("Only an MPX can do this.", code="ROLE_NOT_PERMITTED")
    return admin.mpx_id


def _record(
    po: DevicePo, *, old: str, new: DevicePoStatus, admin: AdminUser, note: str | None
) -> DevicePoStatusHistory:
    return DevicePoStatusHistory(
        device_po_id=po.device_po_id,
        old_status=old,
        new_status=new.value,
        changed_by_admin_id=admin.admin_user_id,
        note=note,
    )


# ---------------------------------------------------------------------------
# Shipment
# ---------------------------------------------------------------------------


async def create_shipment(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, payload: CreateShipmentRequest
) -> ShipmentOut:
    """Dispatch the order. Transition ``DIPROSES`` -> ``DIKIRIM``.

    Every unit must already be attached: shipping an order that is three bundles short
    guarantees the receipt can never be confirmed, because receipt is all-or-nothing.
    Better to refuse here, while the boxes are still in the warehouse.
    """
    _require_dp(admin)
    po = await _get_po_scoped(session, admin, po_id)
    ensure_transition(
        current=po.status.value,
        target=DevicePoStatus.DIKIRIM.value,
        allowed_from={DevicePoStatus.DIPROSES.value},
    )

    attached = (
        await session.scalars(
            select(FwaInventory).where(
                FwaInventory.device_po_id == po_id,
                FwaInventory.status == InventoryStatus.ASSIGNED,
            )
        )
    ).all()
    if len(attached) != po.qty:
        raise ValidationFailedError(
            f"This order needs all {po.qty} units attached before it can ship; "
            f"{len(attached)} are.",
            code="ORDER_NOT_FULLY_ATTACHED",
            details=[{"field": "bundles", "issue": f"attached {len(attached)} of {po.qty}"}],
        )

    now = datetime.now(UTC)
    shipment = Shipment(
        device_po_id=po.device_po_id,
        courier_name=payload.courier_name.strip(),
        awb=payload.awb.strip(),
        shipped_at=now,
        estimated_delivery_date=payload.estimated_delivery_date,
        created_by_admin_id=admin.admin_user_id,
    )
    session.add(shipment)
    await session.flush()

    session.add(
        ShipmentMilestone(
            shipment_id=shipment.shipment_id,
            milestone=ShipmentMilestoneType.SHIPPED,
            occurred_at=now,
            created_by_admin_id=admin.admin_user_id,
        )
    )
    for unit in attached:
        unit.status = InventoryStatus.SHIPPED

    old = po.status.value
    po.status = DevicePoStatus.DIKIRIM
    # The exact string the Riwayat panel renders: "J&T Express | JD0463672772".
    session.add(
        _record(
            po,
            old=old,
            new=po.status,
            admin=admin,
            note=f"{shipment.courier_name} | {shipment.awb}",
        )
    )
    await session.flush()
    return await get_shipment(session, admin, po_id)


async def get_shipment(session: AsyncSession, admin: AdminUser, po_id: uuid.UUID) -> ShipmentOut:
    await _get_po_scoped(session, admin, po_id)
    shipment = await session.scalar(select(Shipment).where(Shipment.device_po_id == po_id))
    if shipment is None:
        raise NotFoundError("This order has not been shipped yet.", code="SHIPMENT_NOT_FOUND")

    rows = (
        await session.scalars(
            select(ShipmentMilestone)
            .where(ShipmentMilestone.shipment_id == shipment.shipment_id)
            .order_by(ShipmentMilestone.occurred_at)
        )
    ).all()
    by_type = {row.milestone: row for row in rows}
    return ShipmentOut(
        shipment_id=shipment.shipment_id,
        device_po_id=shipment.device_po_id,
        courier_name=shipment.courier_name,
        awb=shipment.awb,
        shipped_at=shipment.shipped_at,
        estimated_delivery_date=shipment.estimated_delivery_date,
        delivered_at=shipment.delivered_at,
        # Returned in tracker order rather than insertion order, so the client renders
        # the four steps without sorting them itself.
        milestones=[
            MilestoneOut(
                milestone=step,
                occurred_at=by_type[step].occurred_at,
                note=by_type[step].note,
            )
            for step in MILESTONE_ORDER
            if step in by_type
        ],
    )


async def add_milestone(
    session: AsyncSession, admin: AdminUser, shipment_id: uuid.UUID, payload: MilestoneRequest
) -> ShipmentOut:
    """Advance the tracker. Entered by hand — there is no courier integration in v1."""
    _require_dp(admin)
    shipment = await session.scalar(select(Shipment).where(Shipment.shipment_id == shipment_id))
    if shipment is None:
        raise NotFoundError("This shipment does not exist.", code="SHIPMENT_NOT_FOUND")
    # Scoping is on the order, not the shipment: a DP may only touch its own.
    await _get_po_scoped(session, admin, shipment.device_po_id)

    existing = await session.scalar(
        select(ShipmentMilestone).where(
            ShipmentMilestone.shipment_id == shipment_id,
            ShipmentMilestone.milestone == payload.milestone,
        )
    )
    if existing is not None:
        raise ConflictError(
            f"{payload.milestone.value} has already been recorded for this shipment.",
            code="MILESTONE_ALREADY_RECORDED",
        )

    occurred = payload.occurred_at or datetime.now(UTC)
    session.add(
        ShipmentMilestone(
            shipment_id=shipment_id,
            milestone=payload.milestone,
            occurred_at=occurred,
            note=payload.note,
            created_by_admin_id=admin.admin_user_id,
        )
    )
    if payload.milestone is ShipmentMilestoneType.DELIVERED:
        shipment.delivered_at = occurred

    await session.flush()
    return await get_shipment(session, admin, shipment.device_po_id)


# ---------------------------------------------------------------------------
# Goods receipt
# ---------------------------------------------------------------------------


async def inspect(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, note: str | None
) -> DevicePoStatus:
    """MPX opens the delivery. Transition ``DIKIRIM`` -> ``PERIKSA``."""
    _require_mpx(admin)
    po = await _get_po_scoped(session, admin, po_id)
    ensure_transition(
        current=po.status.value,
        target=DevicePoStatus.PERIKSA.value,
        allowed_from={DevicePoStatus.DIKIRIM.value},
    )
    old = po.status.value
    po.status = DevicePoStatus.PERIKSA
    session.add(_record(po, old=old, new=po.status, admin=admin, note=note))
    return po.status


async def confirm_receipt(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, note: str | None
) -> ReceiptOut:
    """Confirm the whole delivery. Transition ``PERIKSA`` -> ``DITERIMA``.

    All or nothing, by decision. If the box is two units short the order stays at
    ``PERIKSA`` and this refuses: the reshipping happens outside this system, and MPX
    confirms only once the full list is physically present. That is why there is no
    partial state to fall back to — the alternative was an MPX admin personally
    attesting to units they never received.
    """
    _require_mpx(admin)
    po = await _get_po_scoped(session, admin, po_id)
    ensure_transition(
        current=po.status.value,
        target=DevicePoStatus.DITERIMA.value,
        allowed_from={DevicePoStatus.PERIKSA.value},
    )

    units = (
        await session.scalars(
            select(FwaInventory).where(
                FwaInventory.device_po_id == po_id,
                FwaInventory.status == InventoryStatus.SHIPPED,
            )
        )
    ).all()
    if len(units) != po.qty:
        raise ValidationFailedError(
            f"{len(units)} of {po.qty} units are accounted for. Receipt can only be "
            f"confirmed once the full list has arrived.",
            code="RECEIPT_INCOMPLETE",
            details=[{"field": "qty", "issue": f"expected {po.qty}, found {len(units)}"}],
        )

    now = datetime.now(UTC)
    for unit in units:
        unit.status = InventoryStatus.RECEIVED
        # The FIFO key for automatic allocation. Set once, here.
        unit.received_at = now

    receipt = GoodsReceipt(
        device_po_id=po.device_po_id,
        received_by_admin_id=admin.admin_user_id,
        received_at=now,
        qty_received=len(units),
        note=note,
    )
    session.add(receipt)

    old = po.status.value
    po.status = DevicePoStatus.DITERIMA
    po.completed_at = now
    session.add(
        _record(po, old=old, new=po.status, admin=admin, note=f"{len(units)} unit diterima")
    )
    await session.flush()

    return ReceiptOut(
        goods_receipt_id=receipt.goods_receipt_id,
        device_po_id=po.device_po_id,
        status=po.status,
        qty_received=receipt.qty_received,
        received_at=receipt.received_at,
        received_by=admin.full_name,
        note=receipt.note,
    )


async def get_receipt(session: AsyncSession, admin: AdminUser, po_id: uuid.UUID) -> ReceiptOut:
    po = await _get_po_scoped(session, admin, po_id)
    receipt = await session.scalar(select(GoodsReceipt).where(GoodsReceipt.device_po_id == po_id))
    if receipt is None:
        raise NotFoundError("This order has not been received yet.", code="RECEIPT_NOT_FOUND")
    return ReceiptOut(
        goods_receipt_id=receipt.goods_receipt_id,
        device_po_id=po.device_po_id,
        status=po.status,
        qty_received=receipt.qty_received,
        received_at=receipt.received_at,
        received_by=receipt.received_by.full_name if receipt.received_by else None,
        note=receipt.note,
    )


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------


async def stock_summary(session: AsyncSession, admin: AdminUser) -> list[StockLineOut]:
    """The *Stok Tersedia* table.

    Column definitions, because the mockups were arithmetically impossible (issue #5
    showed 20 available + 21 allocated = 10 total):

    * *Tersedia*      = RECEIVED and not yet allocated
    * *Dialokasikan*  = ALLOCATED and not yet activated
    * *Total*         = the two added: everything this MPX still holds

    An activated unit has been sold and belongs to a customer, so it leaves all three.
    """
    mpx_id = _require_mpx(admin)

    rows = (
        await session.execute(
            select(
                FwaInventory.device_model_id,
                DeviceModel.model_code,
                func.count().filter(FwaInventory.status == InventoryStatus.RECEIVED),
                func.count().filter(FwaInventory.status == InventoryStatus.ALLOCATED),
            )
            .select_from(FwaInventory)
            .outerjoin(DeviceModel, DeviceModel.device_model_id == FwaInventory.device_model_id)
            .where(
                FwaInventory.mpx_id == mpx_id,
                FwaInventory.status.in_([InventoryStatus.RECEIVED, InventoryStatus.ALLOCATED]),
            )
            .group_by(FwaInventory.device_model_id, DeviceModel.model_code)
            .order_by(DeviceModel.model_code)
        )
    ).all()

    return [
        StockLineOut(
            device_model_id=model_id,
            device_model_code=model_code,
            available=available,
            allocated=allocated,
            total=available + allocated,
        )
        for model_id, model_code, available, allocated in rows
    ]


async def stock_bundles(
    session: AsyncSession,
    admin: AdminUser,
    *,
    device_model_id: uuid.UUID | None,
    pagination: Pagination,
) -> tuple[list[StockBundleOut], int]:
    """Individual allocatable units, oldest received first.

    The ordering is the same one AUTO allocation uses, so what the Manual picker shows
    at the top is what Otomatis would have taken.
    """
    mpx_id = _require_mpx(admin)

    filters = [
        FwaInventory.mpx_id == mpx_id,
        FwaInventory.status == InventoryStatus.RECEIVED,
    ]
    if device_model_id is not None:
        filters.append(FwaInventory.device_model_id == device_model_id)

    total = int(
        await session.scalar(select(func.count()).select_from(FwaInventory).where(*filters)) or 0
    )
    rows = (
        await session.scalars(
            select(FwaInventory)
            .where(*filters)
            .order_by(FwaInventory.received_at, FwaInventory.msisdn)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
    ).all()

    return [
        StockBundleOut(
            msisdn=row.msisdn,
            imei=row.imei,
            device_model_code=(
                row.device_model.model_code if row.device_model is not None else None
            ),
            brand_code=row.brand_code,
            received_at=row.received_at,
        )
        for row in rows
    ], total


# ---------------------------------------------------------------------------
# Allocation — the only writer of fwa_inventory.allocated_ae_id
# ---------------------------------------------------------------------------


async def _lock_auto(
    session: AsyncSession, *, mpx_id: uuid.UUID, device_model_id: uuid.UUID, qty: int
) -> list[FwaInventory]:
    """Take the ``qty`` oldest received units of one model, locking as we go.

    ``FOR UPDATE SKIP LOCKED`` is what makes two admins allocating the last units at the
    same moment safe: each skips rows the other has claimed rather than blocking on
    them, so one succeeds fully and the other fails cleanly on the count check below —
    instead of both waiting and then handing the same unit to two salesmen.
    """
    return list(
        (
            await session.scalars(
                select(FwaInventory)
                .where(
                    FwaInventory.mpx_id == mpx_id,
                    FwaInventory.device_model_id == device_model_id,
                    FwaInventory.status == InventoryStatus.RECEIVED,
                )
                # The msisdn tiebreaker matters: without a total order, concurrent
                # allocations of the last few units interleave unpredictably.
                .order_by(FwaInventory.received_at, FwaInventory.msisdn)
                .limit(qty)
                .with_for_update(of=FwaInventory, skip_locked=True)
            )
        ).all()
    )


async def allocate(
    session: AsyncSession, admin: AdminUser, payload: CreateAllocationRequest
) -> AllocationDetailOut:
    """Hand stock to an Account Executive.

    **The single writer of ``fwa_inventory.allocated_ae_id``.** Same discipline as
    ``activation.activation_date``, which only the GA feed may set: the value decides
    what a salesman is allowed to sell, so a second writer would be a way to give away
    somebody else's stock.
    """
    mpx_id = _require_mpx(admin)

    ae = await session.scalar(
        select(AccountExecutive).where(AccountExecutive.ae_id == payload.ae_id)
    )
    # An AE drawing stock from another MPX reads as absent, not forbidden.
    if ae is None or ae.mpx_code != (admin.mpx.code if admin.mpx else None):
        raise ValidationFailedError(
            "That Account Executive does not draw stock from this MPX.",
            code="AE_NOT_FOUND",
            details=[{"field": "aeId", "issue": "unknown or not yours"}],
        )

    units: list[FwaInventory] = []

    if payload.mode is AllocationMode.AUTO:
        if not payload.items:
            raise ValidationFailedError(
                "Automatic allocation needs a quantity per device model.",
                code="ITEMS_REQUIRED",
                details=[{"field": "items", "issue": "required in AUTO mode"}],
            )
        for line in payload.items:
            taken = await _lock_auto(
                session, mpx_id=mpx_id, device_model_id=line.device_model_id, qty=line.qty
            )
            if len(taken) != line.qty:
                raise ConflictError(
                    f"Only {len(taken)} of {line.qty} units are available for that model.",
                    code="INSUFFICIENT_STOCK",
                    details=[
                        {
                            "field": "items",
                            "issue": f"requested {line.qty}, available {len(taken)}",
                        }
                    ],
                )
            units.extend(taken)
    else:
        if not payload.msisdns:
            raise ValidationFailedError(
                "Manual allocation needs the units to allocate.",
                code="MSISDNS_REQUIRED",
                details=[{"field": "msisdns", "issue": "required in MANUAL mode"}],
            )
        wanted: list[str] = []
        for raw in payload.msisdns:
            value = normalise_msisdn(raw)
            if value is None:
                raise ValidationFailedError(
                    f"{raw} is not a valid Indonesian MSISDN. Nothing was allocated.",
                    code="INVALID_MSISDN",
                    details=[{"field": "msisdns", "issue": f"invalid: {raw}"}],
                )
            wanted.append(value)

        units = list(
            (
                await session.scalars(
                    select(FwaInventory)
                    .where(
                        FwaInventory.msisdn.in_(wanted),
                        FwaInventory.mpx_id == mpx_id,
                        FwaInventory.status == InventoryStatus.RECEIVED,
                    )
                    .with_for_update(of=FwaInventory)
                )
            ).all()
        )
        if len(units) != len(set(wanted)):
            raise ValidationFailedError(
                "Some of those units are not available to allocate. Nothing was allocated.",
                code="BUNDLE_NOT_ALLOCATABLE",
                details=[
                    {"field": "msisdns", "issue": f"{len(units)} of {len(set(wanted))} available"}
                ],
            )

    now = datetime.now(UTC)
    allocation = StockAllocation(
        mpx_id=mpx_id,
        ae_id=ae.ae_id,
        allocated_by_admin_id=admin.admin_user_id,
        allocated_at=now,
        mode=payload.mode,
        qty=len(units),
        note=payload.note,
    )
    session.add(allocation)
    await session.flush()

    for unit in units:
        # The seam. After this the AE app can see the unit.
        unit.allocated_ae_id = ae.ae_id
        unit.allocated_at = now
        unit.status = InventoryStatus.ALLOCATED
        session.add(
            StockAllocationItem(
                stock_allocation_id=allocation.stock_allocation_id, msisdn=unit.msisdn
            )
        )

    await session.flush()
    return AllocationDetailOut(
        stock_allocation_id=allocation.stock_allocation_id,
        ae_id=ae.ae_id,
        ae_code=ae.ae_code,
        mode=allocation.mode,
        qty=allocation.qty,
        allocated_at=allocation.allocated_at,
        allocated_by=admin.full_name,
        note=allocation.note,
        msisdns=sorted(unit.msisdn for unit in units),
    )


async def list_allocations(
    session: AsyncSession, admin: AdminUser, *, pagination: Pagination
) -> tuple[list[AllocationOut], int]:
    mpx_id = _require_mpx(admin)

    total = int(
        await session.scalar(
            select(func.count())
            .select_from(StockAllocation)
            .where(StockAllocation.mpx_id == mpx_id)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(StockAllocation, AccountExecutive.ae_code, AdminUser.full_name)
            .join(AccountExecutive, AccountExecutive.ae_id == StockAllocation.ae_id)
            .outerjoin(AdminUser, AdminUser.admin_user_id == StockAllocation.allocated_by_admin_id)
            .where(StockAllocation.mpx_id == mpx_id)
            .order_by(StockAllocation.allocated_at.desc())
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
    ).all()

    return [
        AllocationOut(
            stock_allocation_id=row.stock_allocation_id,
            ae_id=row.ae_id,
            ae_code=ae_code,
            mode=row.mode,
            qty=row.qty,
            allocated_at=row.allocated_at,
            allocated_by=full_name,
            note=row.note,
        )
        for row, ae_code, full_name in rows
    ], total
