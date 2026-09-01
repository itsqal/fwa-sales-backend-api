"""Shipment, goods receipt, stock and allocation endpoints — steps 8 and 9.

`POST /admin/allocations` is the end of the whole programme: it is the only writer of
`fwa_inventory.allocated_ae_id`, and the first successful call makes a unit visible to
an Account Executive's barcode scanner.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.core.deps import CurrentAdmin, DbSession
from app.core.pagination import Pagination, get_pagination
from app.schemas.common import Paginated, Wrapped
from app.schemas.fulfilment import (
    AllocationDetailOut,
    AllocationOut,
    CreateAllocationRequest,
    CreateShipmentRequest,
    MilestoneRequest,
    ReceiptOut,
    ReceiptRequest,
    ShipmentOut,
    StockBundleOut,
    StockLineOut,
)
from app.schemas.purchasing import NoteRequest
from app.services import fulfilment as service

po_router = APIRouter(prefix="/device-pos", tags=["Shipment"])
shipment_router = APIRouter(prefix="/shipments", tags=["Shipment"])
stock_router = APIRouter(tags=["Stock & Allocation"])


# ---------------------------------------------------------------------------
# Shipment — the screen the UI review found missing entirely (issue #1)
# ---------------------------------------------------------------------------


@po_router.post(
    "/{poId}/shipment",
    summary="DP ships the order",
    response_model=ShipmentOut,
    responses={409: {"description": "Not in a shippable state"}},
)
async def create_shipment(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: CreateShipmentRequest,
) -> ShipmentOut:
    """Records the courier and AWB, moves the order to `DIKIRIM`, and writes
    `{courier} | {awb}` into the history the Riwayat panel renders.

    Refuses unless every unit is attached: shipping a short order guarantees the
    receipt can never be confirmed, and it is cheaper to find out now.
    """
    return await service.create_shipment(session, admin, po_id, payload)


@po_router.get(
    "/{poId}/shipment",
    summary="Delivery progress for this order",
    response_model=ShipmentOut,
    responses={404: {"description": "Not shipped yet"}},
)
async def get_shipment(
    admin: CurrentAdmin, session: DbSession, po_id: Annotated[uuid.UUID, Path(alias="poId")]
) -> ShipmentOut:
    """Backs the four-step Delivery Progress tracker. Milestones come back in tracker
    order, so the client renders them without sorting."""
    return await service.get_shipment(session, admin, po_id)


@shipment_router.post(
    "/{shipmentId}/milestones",
    summary="Record a delivery milestone",
    response_model=ShipmentOut,
    responses={409: {"description": "Already recorded"}},
)
async def add_milestone(
    admin: CurrentAdmin,
    session: DbSession,
    shipment_id: Annotated[uuid.UUID, Path(alias="shipmentId")],
    payload: MilestoneRequest,
) -> ShipmentOut:
    """Entered by hand by the Device Partner. There is no courier integration in v1, so
    this takes an admin token rather than a service credential."""
    return await service.add_milestone(session, admin, shipment_id, payload)


# ---------------------------------------------------------------------------
# Goods receipt
# ---------------------------------------------------------------------------


@po_router.post(
    "/{poId}/inspect",
    summary="MPX opens the delivery",
    response_model=Wrapped[str],
    tags=["Goods Receipt"],
)
async def inspect(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: NoteRequest | None = None,
) -> Wrapped[str]:
    status = await service.inspect(session, admin, po_id, note=payload.note if payload else None)
    return Wrapped(data=status.value)


@po_router.post(
    "/{poId}/receipt",
    summary="MPX confirms the whole delivery",
    response_model=ReceiptOut,
    tags=["Goods Receipt"],
    responses={
        409: {"description": "Not in a receivable state"},
        422: {"description": "Units missing — RECEIPT_INCOMPLETE"},
    },
)
async def confirm_receipt(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: ReceiptRequest,
) -> ReceiptOut:
    """One final, all-or-nothing confirmation.

    If units are missing this refuses and the order stays at `PERIKSA`. Reshipping is
    handled outside this system; MPX confirms only once the full list has arrived.
    """
    if not payload.confirmed:
        from app.core.errors import ValidationFailedError

        raise ValidationFailedError(
            "Confirm receipt of the delivery to continue.",
            code="CONFIRMATION_REQUIRED",
            details=[{"field": "confirmed", "issue": "must be true"}],
        )
    return await service.confirm_receipt(session, admin, po_id, note=payload.note)


@po_router.get(
    "/{poId}/receipt",
    summary="The receipt for this order",
    response_model=ReceiptOut,
    tags=["Goods Receipt"],
    responses={404: {"description": "Not received yet"}},
)
async def get_receipt(
    admin: CurrentAdmin, session: DbSession, po_id: Annotated[uuid.UUID, Path(alias="poId")]
) -> ReceiptOut:
    return await service.get_receipt(session, admin, po_id)


# ---------------------------------------------------------------------------
# Stock and allocation
# ---------------------------------------------------------------------------


@stock_router.get(
    "/stock",
    summary="Stock this MPX holds, by device model",
    response_model=Wrapped[list[StockLineOut]],
)
async def stock_summary(admin: CurrentAdmin, session: DbSession) -> Wrapped[list[StockLineOut]]:
    """*Tersedia* = received and unallocated; *Dialokasikan* = allocated and not yet
    activated; *Total* = the two added. An activated unit has been sold and leaves all
    three counts."""
    return Wrapped(data=await service.stock_summary(session, admin))


@stock_router.get(
    "/stock/bundles",
    summary="Individual allocatable units, oldest received first",
    response_model=Paginated[StockBundleOut],
)
async def stock_bundles(
    admin: CurrentAdmin,
    session: DbSession,
    pagination: Annotated[Pagination, Depends(get_pagination)],
    device_model_id: Annotated[uuid.UUID | None, Query(alias="deviceModelId")] = None,
) -> Paginated[StockBundleOut]:
    """Backs the Manual picker. Ordered exactly as automatic allocation would take
    them, so the top of the list is what Otomatis would have chosen."""
    items, total = await service.stock_bundles(
        session, admin, device_model_id=device_model_id, pagination=pagination
    )
    return Paginated[StockBundleOut].model_validate(pagination.envelope(items, total))


@stock_router.get(
    "/allocations", summary="Allocations made by this MPX", response_model=Paginated[AllocationOut]
)
async def list_allocations(
    admin: CurrentAdmin,
    session: DbSession,
    pagination: Annotated[Pagination, Depends(get_pagination)],
) -> Paginated[AllocationOut]:
    items, total = await service.list_allocations(session, admin, pagination=pagination)
    return Paginated[AllocationOut].model_validate(pagination.envelope(items, total))


@stock_router.post(
    "/allocations",
    summary="Allocate stock to an Account Executive",
    response_model=AllocationDetailOut,
    status_code=201,
    responses={
        409: {"description": "Insufficient stock"},
        422: {"description": "Unknown AE, or units not allocatable"},
    },
)
async def allocate(
    admin: CurrentAdmin, session: DbSession, payload: CreateAllocationRequest
) -> AllocationDetailOut:
    """**The only writer of `fwa_inventory.allocated_ae_id`**, and the end of the chain:
    after this call the units are visible to that salesman's barcode scanner.

    `AUTO` takes the oldest received units per model — FIFO on *Tgl. Terima* — under
    `FOR UPDATE SKIP LOCKED`, so two admins allocating the last unit produce one success
    and one clean failure rather than two handovers of the same device.
    """
    return await service.allocate(session, admin, payload)
