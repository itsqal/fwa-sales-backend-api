"""MSISDN resolution for the barcode scanner, and this AE's allocated stock.

Since migration 0005 this table also holds units that belong to the supply chain and
not to any salesman: numbers IOH has issued but nobody has paired, bundles in transit
to an MPX, stock received but not yet handed out. None of that is the mobile app's
business, and one of those states carries ``imei IS NULL``, which the AE contract types
as a required string.

:data:`AE_VISIBLE_STATUSES` is the single place that decides what an AE may see. Every
read in this module goes through it. Widening it is a contract change, not a tweak.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.pagination import Pagination
from app.models.activation import Activation
from app.models.enums import IneligibilityReason, InventoryStatus
from app.models.inventory import FwaInventory
from app.schemas.inventory import DeviceModelOut, InventoryItemOut

# The only statuses the mobile app may ever see. A unit reaches ALLOCATED when an MPX
# admin hands it to this AE; everything before that is the supply chain's business and
# is invisible here — including MSISDN_ISSUED, which has no IMEI at all.
AE_VISIBLE_STATUSES: Final[frozenset[InventoryStatus]] = frozenset(
    {
        InventoryStatus.ALLOCATED,
        InventoryStatus.ACTIVATED,
        InventoryStatus.RETURNED,
        InventoryStatus.BLOCKED,
        # CONSUMED is written by the activation path itself, on every unit an AE sells.
        # The Supply Chain spec calls it "now unused" and omits it from the prescribed
        # filter; that is wrong. Dropping it would make a unit vanish the instant it is
        # activated, so rescanning the same box would answer "not a registered unit"
        # instead of "already activated" — losing the duplicate-activation guard that
        # the 409 depends on.
        InventoryStatus.CONSUMED,
        # Deprecated, but live rows still carry it and it predates the supply chain.
        InventoryStatus.AVAILABLE,
    }
)


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
    # A unit still in the supply chain belongs to nobody, so the ownership test below
    # would already reject it. It is named separately because the reasons differ: one
    # is "this is someone else's", the other is "this has not been handed out yet", and
    # a caller that ever reaches this with such a unit has a bug worth seeing.
    if item.status not in AE_VISIBLE_STATUSES:
        return IneligibilityReason.NOT_ALLOCATED_TO_YOU
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
    if item.imei is None:
        # Unreachable through any AE route: every caller filters to AE_VISIBLE_STATUSES,
        # and `imei_required_once_paired` confines a NULL IMEI to MSISDN_ISSUED, which
        # is not in that set. Raising rather than coercing to a blank means a future
        # caller that forgets the filter fails loudly here, instead of rendering an
        # empty IMEI on a scanner screen. The MSISDN is masked; see CLAUDE.md §9.
        raise ValueError(f"Unit ...{item.msisdn[-4:]} has no IMEI and is not visible to an AE.")
    return InventoryItemOut(
        msisdn=item.msisdn,
        iccid=item.iccid,
        imei=item.imei,
        device_model=(
            DeviceModelOut(
                model_code=item.device_model.model_code,
                brand=item.device_model.brand,
                network_generation=item.device_model.network_generation,
            )
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
    # The status check is not a refinement of the None check, it is the same answer to
    # a different question. This is the one AE read with no ownership filter, so a unit
    # still moving through the supply chain would otherwise resolve here — and a unit at
    # MSISDN_ISSUED has no IMEI, which would fail serialisation and return 500 to a
    # scanner. To an AE such a number simply does not exist yet.
    if item is None or item.status not in AE_VISIBLE_STATUSES:
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
    # allocated_ae_id already excludes supply-chain rows, which carry NULL. The status
    # filter is here because nothing in the database enforces that pairing: a unit that
    # somehow carried an owner while still in transit must not appear as sellable stock.
    filters = [
        FwaInventory.allocated_ae_id == ae_id,
        FwaInventory.status.in_(AE_VISIBLE_STATUSES),
    ]
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
                # Sellable stock only, and never anything outside AE_VISIBLE_STATUSES.
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
