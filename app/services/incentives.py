"""Incentive accrual.

**This module invents nothing.** Amounts and conditions are an open business decision
(CLAUDE.md §11.1), so the engine is entirely data-driven: it reads ``incentive_rule``
rows and writes ``incentive_ledger`` entries. With no rules configured, an AE accrues
nothing and the "Insentif" tile reads zero — which is the truthful answer until the
business supplies the numbers, and is far better than a plausible constant that quietly
becomes the de-facto policy.

Money is a whole-rupiah integer at the API edge and a ``Decimal`` in between. Never a
float: a payment that is off by a rounding error is a payroll incident.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activation import Activation
from app.models.enums import IncentiveEventType, LedgerStatus
from app.models.incentive import IncentiveLedger, IncentiveRule

logger = logging.getLogger(__name__)


def to_whole_rupiah(amount: Decimal) -> int:
    """Rupiah has no minor unit in practice; the column keeps 2 dp for safety."""
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def accrue_for_activation(
    session: AsyncSession, *, activation: Activation, earned_on: date
) -> int:
    """Write ledger entries for a confirmed Gross Add. Returns the number written.

    Called only from the GA ingestion path, because the available evidence is that
    incentive tiers on *confirmed* GA rather than on submissions — a submitted unit
    that never goes live has earned nobody anything.
    """
    rules = (
        await session.scalars(
            select(IncentiveRule).where(
                IncentiveRule.event_type == IncentiveEventType.ACTIVATION,
                IncentiveRule.effective_from <= earned_on,
                (IncentiveRule.effective_to.is_(None)) | (IncentiveRule.effective_to >= earned_on),
            )
        )
    ).all()

    written = 0
    for rule in rules:
        if rule.conditions:
            # The condition language has never been specified. Applying a rule whose
            # conditions we cannot evaluate would pay out on terms nobody agreed to.
            logger.warning(
                "Skipping incentive rule %s: it carries conditions and the condition "
                "language is not yet defined.",
                rule.rule_id,
            )
            continue

        statement = (
            pg_insert(IncentiveLedger)
            .values(
                ae_id=activation.ae_id,
                rule_id=rule.rule_id,
                activation_id=activation.activation_id,
                # Always populated, never NULL: uq_ledger_event is NULLS DISTINCT, so a
                # NULL here would let a replayed GA event accrue the same payout twice.
                customer_id=activation.customer_id,
                amount_idr=rule.amount_idr,
                earned_date=earned_on,
                period_ym=earned_on.strftime("%Y-%m"),
                status=LedgerStatus.ACCRUED,
            )
            .on_conflict_do_nothing(constraint="uq_ledger_event")
            .returning(IncentiveLedger.ledger_id)
        )
        if await session.scalar(statement) is not None:
            written += 1

    return written


async def void_for_activation(session: AsyncSession, *, activation_id: int) -> None:
    """Reverse accruals when the GA feed reports a failure after an earlier success.

    The ledger is append-only in spirit: entries are marked VOID rather than deleted,
    so a payout dispute can still be reconstructed.
    """
    entries = (
        await session.scalars(
            select(IncentiveLedger).where(
                IncentiveLedger.activation_id == activation_id,
                IncentiveLedger.status == LedgerStatus.ACCRUED,
            )
        )
    ).all()
    for entry in entries:
        entry.status = LedgerStatus.VOID


async def sum_for_period(session: AsyncSession, *, ae_id: uuid.UUID, start: date, end: date) -> int:
    total = await session.scalar(
        select(func.coalesce(func.sum(IncentiveLedger.amount_idr), 0)).where(
            IncentiveLedger.ae_id == ae_id,
            IncentiveLedger.earned_date >= start,
            IncentiveLedger.earned_date <= end,
            IncentiveLedger.status != LedgerStatus.VOID,
        )
    )
    return to_whole_rupiah(Decimal(total or 0))
