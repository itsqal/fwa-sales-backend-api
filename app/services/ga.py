"""Gross Add ingestion from GCP.

This is the only code path in the system permitted to write ``activation_date``. It is
what flips the red *Not Activated* badge green, and it is the trigger for incentive
accrual — which is why it is a service-credential endpoint and not an AE-token one.

A feed is a firehose. An event for an MSISDN nobody here has ever activated is normal,
not an error: it is counted as unmatched and the batch continues.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.msisdn import mask_msisdn
from app.models.activation import Activation
from app.models.enums import ActivationStatus, InventoryStatus
from app.models.inventory import FwaInventory
from app.schemas.internal import GaEvent, GaIngestResult, GaOutcome
from app.services.incentives import accrue_for_activation, void_for_activation

logger = logging.getLogger(__name__)


async def ingest(
    session: AsyncSession, settings: Settings, *, events: list[GaEvent]
) -> GaIngestResult:
    matched = 0
    unmatched = 0

    for event in events:
        # `of=Activation`: the model eager-loads its customer with a LEFT JOIN, and
        # PostgreSQL refuses FOR UPDATE on the nullable side of an outer join.
        activation = await session.scalar(
            select(Activation)
            .where(Activation.msisdn == event.msisdn)
            .with_for_update(of=Activation)
        )
        if activation is None:
            unmatched += 1
            logger.info("GA event for unknown MSISDN %s", mask_msisdn(event.msisdn))
            continue

        matched += 1
        activation.ga_synced_at = datetime.now(UTC)

        if event.outcome is GaOutcome.ACTIVATED:
            await _mark_activated(session, settings, activation=activation, event=event)
        else:
            await _mark_failed(session, activation=activation)

    return GaIngestResult(matched=matched, unmatched=unmatched)


async def _mark_activated(
    session: AsyncSession,
    settings: Settings,
    *,
    activation: Activation,
    event: GaEvent,
) -> None:
    already_live = activation.status is ActivationStatus.ACTIVATED

    activation.status = ActivationStatus.ACTIVATED
    activation.activation_date = event.activation_date

    item = await session.get(FwaInventory, activation.msisdn)
    if item is not None:
        item.status = InventoryStatus.ACTIVATED

    if already_live:
        # Re-delivery of an event we have already processed. The ledger's unique
        # constraint would reject a duplicate anyway; skipping is just cheaper.
        return

    # The GA date is authoritative for when the money was earned, and it is resolved
    # in the business timezone so a late-night activation lands on the right day.
    earned_on = event.activation_date.astimezone(settings.tz).date()
    await session.flush()
    await accrue_for_activation(session, activation=activation, earned_on=earned_on)


async def _mark_failed(session: AsyncSession, *, activation: Activation) -> None:
    activation.status = ActivationStatus.FAILED
    # ck_act_ga only constrains ACTIVATED rows, but a failed activation carrying a GA
    # date would be a lie in every report that reads it.
    activation.activation_date = None
    await void_for_activation(session, activation_id=activation.activation_id)

    item = await session.get(FwaInventory, activation.msisdn)
    if item is not None:
        # The unit is not live and is not sellable either — it needs a human decision,
        # so it stays CONSUMED rather than silently returning to stock.
        item.status = InventoryStatus.CONSUMED
