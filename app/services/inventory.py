"""MSISDN resolution for the barcode scanner, and this AE's allocated stock."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.pagination import Pagination
from app.models.activation import Activation
from app.models.enums import IneligibilityReason, InventoryStatus
from app.models.inventory import FwaInventory
from app.schemas.inventory import DeviceModelOut, InventoryItemOut


def assess_eligibility(
    item: FwaInventory,
    *,
    ae_id: uuid.UUID,
    already_activated: bool,
) -> IneligibilityReason | None:
    """Why this AE may not activate this MSISDN right now, or ``None`` if they may.

    Ownership is checked first so a number belonging to another AE reveals nothing
    beyond "not yours".
    """
    if item.allocated_ae_id != ae_id:
        return IneligibilityReason.NOT_ALLOCATED_TO_YOU
    if already_activated or item.status in {InventoryStatus.CONSUMED, InventoryStatus.ACTIVATED}:
        return IneligibilityReason.ALREADY_ACTIVATED
    if item.status is InventoryStatus.BLOCKED:
        return IneligibilityReason.BLOCKED
    if item.status is InventoryStatus.RETURNED:
        return IneligibilityReason.RETURNED
    return None


def to_inventory_out(item: FwaInventory, *, reason: IneligibilityReason | None) -> InventoryItemOut:
    return InventoryItemOut(
        msisdn=item.msisdn,
        iccid=item.iccid,
        imei=item.imei,
        device_model=(
            DeviceModelOut(model_code=item.device_model.model_code, brand=item.device_model.brand)
            if item.device_model is not None
            else None
        ),
        status=item.status,
        eligible=reason is None,
        reason=reason,
    )


async def lookup_msisdn(
    session: AsyncSession, *, ae_id: uuid.UUID, msisdn: str
) -> InventoryItemOut:
    item = await session.get(FwaInventory, msisdn)
    if item is None:
        raise NotFoundError(
            "This number is not a registered HiFi AIR unit.",
            code="MSISDN_NOT_FOUND",
        )

    already_activated = bool(
        await session.scalar(select(Activation.activation_id).where(Activation.msisdn == msisdn))
    )
    reason = assess_eligibility(item, ae_id=ae_id, already_activated=already_activated)
    return to_inventory_out(item, reason=reason)


async def list_my_inventory(
    session: AsyncSession,
    *,
    ae_id: uuid.UUID,
    status: InventoryStatus | None,
    pagination: Pagination,
) -> tuple[list[InventoryItemOut], int]:
    filters = [FwaInventory.allocated_ae_id == ae_id]
    if status is not None:
        filters.append(FwaInventory.status == status)

    total = await session.scalar(select(func.count()).select_from(FwaInventory).where(*filters))

    items = (
        await session.scalars(
            select(FwaInventory)
            .where(*filters)
            .order_by(FwaInventory.status, FwaInventory.msisdn)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
    ).all()

    sold = set(
        (
            await session.scalars(
                select(Activation.msisdn).where(
                    Activation.msisdn.in_([item.msisdn for item in items])
                )
            )
        ).all()
    )

    return [
        to_inventory_out(
            item,
            reason=assess_eligibility(item, ae_id=ae_id, already_activated=item.msisdn in sold),
        )
        for item in items
    ], int(total or 0)


async def fetch_allocated_stock(
    session: AsyncSession, *, ae_id: uuid.UUID, limit: int
) -> list[InventoryItemOut]:
    """Stock payload for the offline bootstrap — only what is still sellable."""
    items = (
        await session.scalars(
            select(FwaInventory)
            .where(
                FwaInventory.allocated_ae_id == ae_id,
                FwaInventory.status.in_([InventoryStatus.ALLOCATED, InventoryStatus.AVAILABLE]),
            )
            .order_by(FwaInventory.msisdn)
            .limit(limit)
        )
    ).all()

    sold = set(
        (
            await session.scalars(
                select(Activation.msisdn).where(
                    Activation.msisdn.in_([item.msisdn for item in items])
                )
            )
        ).all()
    )

    return [
        to_inventory_out(
            item,
            reason=assess_eligibility(item, ae_id=ae_id, already_activated=item.msisdn in sold),
        )
        for item in items
    ]
