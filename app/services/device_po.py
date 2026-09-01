"""Device purchase orders: MPX orders, the Device Partner fulfils.

The mirror image of ``msisdn_po``: an MPX raises the order and a DP acts on it, so the
scoping rule points the other way. Attaching bundles is where this flow meets
``fwa_inventory`` — the DP commits specific paired units to a specific order, and from
that point the unit knows which MPX it is going to.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError, ValidationFailedError
from app.core.msisdn import normalise_msisdn
from app.core.pagination import Pagination
from app.models.admin import AdminUser
from app.models.commercial import Address, Brand
from app.models.enums import AdminRole, DevicePoStatus, InventoryStatus
from app.models.inventory import DeviceModel, FwaInventory
from app.models.organisation import DevicePartner
from app.models.purchasing import DevicePo, DevicePoStatusHistory, MsisdnPo
from app.schemas.purchasing import (
    AddressOut,
    AttachBundlesResultOut,
    AttachBundlesValidationOut,
    BundleOut,
    CreateDevicePoRequest,
    DevicePoDetailOut,
    DevicePoOut,
    StatusHistoryOut,
)
from app.schemas.reference import BrandOut, DevicePartnerRefOut, MpxRefOut
from app.services.supply_common import (
    ensure_transition,
    next_device_po_code,
    require_exact_count,
    require_no_duplicates,
)

# ---------------------------------------------------------------------------
# Scoping — golden rule 7
# ---------------------------------------------------------------------------


def _scope(statement: Select[Any], admin: AdminUser) -> Select[Any]:
    if admin.role is AdminRole.MPX_ADMIN:
        return statement.where(DevicePo.mpx_id == admin.mpx_id)
    if admin.role is AdminRole.DP_ADMIN:
        return statement.where(DevicePo.device_partner_id == admin.device_partner_id)
    if admin.role is AdminRole.IOH_ADMIN:
        return statement
    raise ForbiddenError(
        "Your role does not have access to this action.", code="ROLE_NOT_PERMITTED"
    )


async def _get_scoped(session: AsyncSession, admin: AdminUser, po_id: uuid.UUID) -> DevicePo:
    statement = _scope(select(DevicePo), admin).where(DevicePo.device_po_id == po_id)
    po: DevicePo | None = await session.scalar(statement)
    if po is None:
        raise NotFoundError("This purchase order does not exist.", code="PO_NOT_FOUND")
    return po


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


async def _qty_attached(session: AsyncSession, po_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(FwaInventory).where(FwaInventory.device_po_id == po_id)
        )
        or 0
    )


def _to_out(po: DevicePo, *, qty_attached: int) -> DevicePoOut:
    return DevicePoOut(
        device_po_id=po.device_po_id,
        po_code=po.po_code,
        mpx=MpxRefOut(
            mpx_id=po.mpx.mpx_id,
            code=po.mpx.code,
            name=po.mpx.name,
            legal_name=po.mpx.legal_name,
            circle=po.mpx.circle,
            region_code=po.mpx.region.region_code if po.mpx.region is not None else None,
        ),
        device_partner=DevicePartnerRefOut(
            device_partner_id=po.device_partner.device_partner_id,
            code=po.device_partner.code,
            name=po.device_partner.name,
        ),
        device_model_code=po.device_model.model_code,
        brand=BrandOut(
            code=po.brand.code,
            display_name=po.brand.display_name,
            outlet_name=po.brand.outlet_name,
            sort_order=po.brand.sort_order,
        ),
        qty=po.qty,
        qty_attached=qty_attached,
        unit_price_idr=po.unit_price_idr,
        total_idr=po.total_idr,
        status=po.status,
        pic_name=po.pic_name,
        pic_phone=po.pic_phone,
        note=po.note,
        rejected_reason=po.rejected_reason,
        submitted_at=po.submitted_at,
        accepted_at=po.accepted_at,
        completed_at=po.completed_at,
    )


def _address_out(address: Address) -> AddressOut:
    return AddressOut(
        address_id=address.address_id,
        label=address.label,
        recipient_name=address.recipient_name,
        recipient_phone=address.recipient_phone,
        line1=address.line1,
        kelurahan=address.kelurahan,
        kecamatan=address.kecamatan,
        city=address.city,
        province=address.province,
        postal_code=address.postal_code,
        latitude=address.latitude,
        longitude=address.longitude,
        gmaps_url=address.gmaps_url,
        is_default=address.is_default,
    )


def _record(
    po: DevicePo, *, old: str | None, new: DevicePoStatus, admin: AdminUser, note: str | None
) -> DevicePoStatusHistory:
    return DevicePoStatusHistory(
        device_po_id=po.device_po_id,
        old_status=old,
        new_status=new.value,
        changed_by_admin_id=admin.admin_user_id,
        note=note,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_pos(
    session: AsyncSession,
    admin: AdminUser,
    *,
    status: DevicePoStatus | None,
    q: str | None,
    pagination: Pagination,
) -> tuple[list[DevicePoOut], int]:
    filters = []
    if status is not None:
        filters.append(DevicePo.status == status)
    if q:
        filters.append(DevicePo.po_code.ilike(f"%{q.strip()}%"))

    total = int(
        await session.scalar(
            _scope(select(func.count()).select_from(DevicePo), admin).where(*filters)
        )
        or 0
    )
    rows = (
        await session.scalars(
            _scope(select(DevicePo), admin)
            .where(*filters)
            .order_by(DevicePo.submitted_at.desc())
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
    ).all()

    counted = (
        await session.execute(
            select(FwaInventory.device_po_id, func.count())
            .where(FwaInventory.device_po_id.in_([row.device_po_id for row in rows]))
            .group_by(FwaInventory.device_po_id)
        )
    ).all()
    attached: dict[uuid.UUID, int] = {po_id: count for po_id, count in counted if po_id is not None}
    return [_to_out(row, qty_attached=attached.get(row.device_po_id, 0)) for row in rows], total


async def get_po(session: AsyncSession, admin: AdminUser, po_id: uuid.UUID) -> DevicePoDetailOut:
    po = await _get_scoped(session, admin, po_id)
    history = (
        await session.scalars(
            select(DevicePoStatusHistory)
            .where(DevicePoStatusHistory.device_po_id == po_id)
            # See the note in msisdn_po: now() is transaction start time, so a
            # timestamp alone does not order rows written together.
            .order_by(DevicePoStatusHistory.changed_at, DevicePoStatusHistory.history_id)
        )
    ).all()
    base = _to_out(po, qty_attached=await _qty_attached(session, po_id))
    return DevicePoDetailOut(
        **base.model_dump(),
        address=_address_out(po.address),
        status_history=[
            StatusHistoryOut(
                changed_at=row.changed_at,
                old_status=row.old_status,
                new_status=row.new_status,
                changed_by=row.changed_by.full_name if row.changed_by is not None else None,
                note=row.note,
            )
            for row in history
        ],
    )


async def list_bundles(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID
) -> list[BundleOut]:
    """Backs the *Detail IMEI & MSISDN* modal."""
    await _get_scoped(session, admin, po_id)
    rows = (
        await session.scalars(
            select(FwaInventory)
            .where(FwaInventory.device_po_id == po_id)
            .order_by(FwaInventory.msisdn)
        )
    ).all()
    return [
        BundleOut(
            msisdn=row.msisdn,
            imei=row.imei,
            device_model_code=(
                row.device_model.model_code if row.device_model is not None else None
            ),
            brand_code=row.brand_code,
            status=row.status.value,
            paired_at=row.paired_at,
            received_at=row.received_at,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# MPX: create and cancel
# ---------------------------------------------------------------------------


async def create_po(
    session: AsyncSession, admin: AdminUser, payload: CreateDevicePoRequest
) -> DevicePoOut:
    if admin.role is not AdminRole.MPX_ADMIN or admin.mpx is None:
        raise ForbiddenError("Only an MPX can raise a device order.", code="ROLE_NOT_PERMITTED")

    partner = await session.get(DevicePartner, payload.device_partner_id)
    if partner is None:
        raise ValidationFailedError(
            "That Device Partner does not exist.",
            code="DEVICE_PARTNER_NOT_FOUND",
            details=[{"field": "devicePartnerId", "issue": "unknown"}],
        )

    model = await session.get(DeviceModel, payload.device_model_id)
    if model is None or not model.is_active:
        raise ValidationFailedError(
            "That device model is not available.",
            code="DEVICE_MODEL_NOT_FOUND",
            details=[{"field": "deviceModelId", "issue": "unknown or inactive"}],
        )
    if model.list_price_idr is None:
        # Deliberate: a model with no confirmed price cannot be ordered at a guessed
        # one. Same discipline as an uncategorised network_generation accruing nothing.
        raise ValidationFailedError(
            f"{model.model_code} has no confirmed price and cannot be ordered yet.",
            code="MODEL_NOT_PRICED",
            details=[{"field": "deviceModelId", "issue": "list_price_idr is not set"}],
        )

    brand = await session.get(Brand, payload.brand_code)
    if brand is None or not brand.is_active:
        raise ValidationFailedError(
            "That brand is not available.",
            code="BRAND_NOT_FOUND",
            details=[{"field": "brandCode", "issue": "unknown or inactive"}],
        )

    address = await session.get(Address, payload.address_id)
    # An address belonging to another MPX must read as absent, not as forbidden.
    if address is None or address.mpx_id != admin.mpx_id or not address.is_active:
        raise ValidationFailedError(
            "That delivery address does not exist.",
            code="ADDRESS_NOT_FOUND",
            details=[{"field": "addressId", "issue": "unknown or not yours"}],
        )

    po = DevicePo(
        po_code=await next_device_po_code(
            session,
            circle=admin.mpx.circle.value if admin.mpx.circle is not None else None,
            brand_code=brand.code,
            model_code=model.model_code,
            on=datetime.now(UTC).date(),
        ),
        mpx_id=admin.mpx_id,
        device_partner_id=partner.device_partner_id,
        device_model_id=model.device_model_id,
        brand_code=brand.code,
        qty=payload.qty,
        # Snapshot. A later catalogue change must not rewrite this order.
        unit_price_idr=model.list_price_idr,
        total_idr=model.list_price_idr * payload.qty,
        address_id=address.address_id,
        pic_name=payload.pic_name,
        pic_phone=payload.pic_phone,
        note=payload.note,
        status=DevicePoStatus.DIAJUKAN,
        created_by_admin_id=admin.admin_user_id,
    )
    session.add(po)
    await session.flush()
    session.add(_record(po, old=None, new=DevicePoStatus.DIAJUKAN, admin=admin, note=payload.note))
    await session.refresh(po)
    return _to_out(po, qty_attached=0)


async def cancel_po(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, note: str | None
) -> DevicePoOut:
    if admin.role is not AdminRole.MPX_ADMIN:
        raise ForbiddenError("Only the ordering MPX can cancel.", code="ROLE_NOT_PERMITTED")
    po = await _get_scoped(session, admin, po_id)
    ensure_transition(
        current=po.status.value,
        target=DevicePoStatus.DIBATALKAN.value,
        allowed_from={DevicePoStatus.DIAJUKAN.value},
    )
    old = po.status.value
    po.status = DevicePoStatus.DIBATALKAN
    session.add(_record(po, old=old, new=po.status, admin=admin, note=note))
    return _to_out(po, qty_attached=await _qty_attached(session, po_id))


# ---------------------------------------------------------------------------
# DP: accept, reject, attach bundles
# ---------------------------------------------------------------------------


def _require_dp(admin: AdminUser) -> None:
    if admin.role is not AdminRole.DP_ADMIN:
        raise ForbiddenError(
            "Only the supplying Device Partner can act on this order.",
            code="ROLE_NOT_PERMITTED",
        )


async def accept_po(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, note: str | None
) -> DevicePoOut:
    _require_dp(admin)
    po = await _get_scoped(session, admin, po_id)
    ensure_transition(
        current=po.status.value,
        target=DevicePoStatus.DIPROSES.value,
        allowed_from={DevicePoStatus.DIAJUKAN.value},
    )
    old = po.status.value
    po.status = DevicePoStatus.DIPROSES
    po.accepted_by_admin_id = admin.admin_user_id
    po.accepted_at = datetime.now(UTC)
    session.add(_record(po, old=old, new=po.status, admin=admin, note=note))
    return _to_out(po, qty_attached=await _qty_attached(session, po_id))


async def reject_po(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, reason: str
) -> DevicePoOut:
    _require_dp(admin)
    po = await _get_scoped(session, admin, po_id)
    ensure_transition(
        current=po.status.value,
        target=DevicePoStatus.DITOLAK.value,
        allowed_from={DevicePoStatus.DIAJUKAN.value},
    )
    old = po.status.value
    po.status = DevicePoStatus.DITOLAK
    po.rejected_reason = reason
    po.completed_at = datetime.now(UTC)
    session.add(_record(po, old=old, new=po.status, admin=admin, note=reason))
    return _to_out(po, qty_attached=await _qty_attached(session, po_id))


async def _check_bundles(
    session: AsyncSession, po: DevicePo, admin: AdminUser, msisdns: list[str]
) -> tuple[list[str], list[dict[str, str]]]:
    """Which of these units this DP may legitimately commit to this order.

    A bundle qualifies only if it is paired, unattached, and reached this Device Partner
    through its own MSISDN PO. The last condition is the one that matters: without it a
    DP could attach a competitor's stock to its own order.
    """
    errors: list[dict[str, str]] = []
    normalised: list[str] = []
    seen: set[str] = set()

    for raw in msisdns:
        value = normalise_msisdn(raw)
        if value is None:
            errors.append({"value": raw, "issue": "not a valid Indonesian MSISDN"})
            continue
        if value in seen:
            errors.append({"value": value, "issue": "duplicated in this batch"})
            continue
        seen.add(value)
        normalised.append(value)

    if not normalised:
        return [], errors

    eligible = {
        row.msisdn
        for row in (
            await session.scalars(
                select(FwaInventory)
                .join(MsisdnPo, MsisdnPo.msisdn_po_id == FwaInventory.msisdn_po_id)
                .where(
                    FwaInventory.msisdn.in_(normalised),
                    FwaInventory.status == InventoryStatus.PAIRED,
                    FwaInventory.device_po_id.is_(None),
                    MsisdnPo.device_partner_id == admin.device_partner_id,
                )
            )
        ).all()
    }
    for value in normalised:
        if value not in eligible:
            errors.append({"value": value, "issue": "not an unattached paired bundle of yours"})

    return [value for value in normalised if value in eligible], errors


async def validate_bundles(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, msisdns: list[str]
) -> AttachBundlesValidationOut:
    _require_dp(admin)
    po = await _get_scoped(session, admin, po_id)
    accepted, errors = await _check_bundles(session, po, admin, msisdns)
    return AttachBundlesValidationOut(
        ok=not errors and len(accepted) == po.qty,
        expected=po.qty,
        received=len(msisdns),
        accepted=accepted,
        errors=errors,
    )


async def attach_bundles(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, msisdns: list[str]
) -> AttachBundlesResultOut:
    """Commit exactly ``qty`` bundles to this order. All or nothing.

    Each unit picks up ``device_po_id`` and the destination ``mpx_id`` here, and moves
    to ASSIGNED. That is the point at which the order stops being a quantity and starts
    being a specific set of physical devices.
    """
    _require_dp(admin)
    po = await _get_scoped(session, admin, po_id)
    ensure_transition(
        current=po.status.value,
        target="ASSIGNED",
        allowed_from={DevicePoStatus.DIPROSES.value},
    )

    accepted, errors = await _check_bundles(session, po, admin, msisdns)
    if errors:
        raise ValidationFailedError(
            "Some bundles could not be attached. Nothing was saved.",
            code="BUNDLES_REJECTED",
            details=[{"field": row["value"], "issue": row["issue"]} for row in errors[:10]],
        )
    require_no_duplicates(accepted, field="msisdns", code="DUPLICATE_MSISDN")
    require_exact_count(len(accepted), po.qty, field="bundles", code="QTY_MISMATCH")

    # FOR UPDATE: two DP admins attaching the same bundle to two different orders must
    # not both succeed. SKIP LOCKED is deliberately not used — a contended row here
    # means a real conflict the operator should see, not one to route around.
    units = (
        await session.scalars(
            select(FwaInventory)
            .where(
                FwaInventory.msisdn.in_(accepted),
                FwaInventory.status == InventoryStatus.PAIRED,
                FwaInventory.device_po_id.is_(None),
            )
            .with_for_update(of=FwaInventory)
        )
    ).all()
    if len(units) != po.qty:
        raise ValidationFailedError(
            "Some bundles were attached to another order first. Nothing was saved.",
            code="BUNDLE_ALREADY_ATTACHED",
            details=[{"field": "msisdns", "issue": f"only {len(units)} of {po.qty} available"}],
        )

    for unit in units:
        unit.device_po_id = po.device_po_id
        unit.mpx_id = po.mpx_id
        unit.device_model_id = po.device_model_id
        unit.status = InventoryStatus.ASSIGNED

    # Deliberately no status-history row. Attaching bundles is not a transition — the
    # order stays at DIPROSES until it ships — and writing a same-status row would make
    # the Riwayat panel render "DIPROSES" twice, which reads as a bug to the three
    # companies looking at it. What was attached is on fwa_inventory, where the unit
    # state belongs; how far along the order is, is qtyAttached.
    await session.flush()
    return AttachBundlesResultOut(
        device_po_id=po.device_po_id, status=po.status, attached=len(units)
    )
