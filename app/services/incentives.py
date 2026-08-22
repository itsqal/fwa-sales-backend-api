"""Incentive accrual.

**This module invents nothing.** It reads ``incentive_rule`` rows and writes
``incentive_ledger`` entries; no amount is hard-coded here. With no rules configured an
AE accrues nothing and the "Insentif" tile reads zero, which stays the truthful answer
rather than a plausible constant quietly becoming de-facto policy.

The business settled the AE activation tiers on 2026-08-22: a confirmed Gross Add on a
4G modem accrues Rp 35.000, one on a 5G modem Rp 135.000. Those live as two rows
inserted by migration 0002, distinguished by their ``conditions`` predicate — see
:data:`SUPPORTED_CONDITIONS` for the only key the engine can evaluate. Everything else
about incentive (targets, tiering on volume) remains undecided.

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
from app.models.enums import IncentiveEventType, LedgerStatus, NetworkGeneration
from app.models.incentive import IncentiveLedger, IncentiveRule
from app.models.inventory import DeviceModel, FwaInventory

logger = logging.getLogger(__name__)

#: The condition key selecting an incentive tier by the radio generation of the CPE.
CONDITION_NETWORK_GENERATION = "networkGeneration"

#: Every condition key the engine knows how to evaluate. A rule carrying anything else
#: is skipped rather than guessed at: paying out on terms nobody agreed to is worse
#: than not paying at all, and the skip is logged so it surfaces.
SUPPORTED_CONDITIONS = frozenset({CONDITION_NETWORK_GENERATION})


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

    # Resolved at most once per activation, and only when some rule actually asks.
    generation = (
        await _network_generation_for(session, activation=activation)
        if any(CONDITION_NETWORK_GENERATION in rule.conditions for rule in rules)
        else None
    )

    written = 0
    for rule in rules:
        if not _rule_applies(rule, activation=activation, generation=generation):
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


def _rule_applies(
    rule: IncentiveRule, *, activation: Activation, generation: NetworkGeneration | None
) -> bool:
    """Evaluate a rule's ``conditions`` predicate. An empty predicate always matches.

    Every path that cannot reach a confident *yes* returns ``False`` and says why in the
    log. Silence here would be an unexplained missing payout; a wrong ``True`` would be
    money paid on terms nobody agreed to.
    """
    unsupported = set(rule.conditions) - SUPPORTED_CONDITIONS
    if unsupported:
        logger.warning(
            "Skipping incentive rule %s: unsupported condition key(s) %s.",
            rule.rule_id,
            sorted(unsupported),
        )
        return False

    raw = rule.conditions.get(CONDITION_NETWORK_GENERATION)
    if raw is None:
        return True

    try:
        wanted = NetworkGeneration(raw)
    except ValueError:
        logger.warning(
            "Skipping incentive rule %s: %r is not a known network generation.",
            rule.rule_id,
            raw,
        )
        return False

    if generation is None:
        # The catalogue entry behind this unit has never been categorised, so we cannot
        # tell which tier it earns. Accruing nothing is recoverable — correct the
        # device_model row and re-deliver the GA event; paying the wrong tier is not.
        logger.warning(
            "Activation %s accrued nothing under rule %s: its device model has no "
            "network_generation set.",
            activation.activation_id,
            rule.rule_id,
        )
        return False

    return generation is wanted


async def _network_generation_for(
    session: AsyncSession, *, activation: Activation
) -> NetworkGeneration | None:
    """Read the tier from master data rather than from the activation row.

    ``activation.device_model_code`` is a display snapshot; the catalogue is where the
    categorisation lives. The amount, once accrued, is frozen on the ledger entry, so a
    later correction to the catalogue never rewrites a payout that has already been made.
    """
    return await session.scalar(
        select(DeviceModel.network_generation)
        .join(FwaInventory, FwaInventory.device_model_id == DeviceModel.device_model_id)
        .where(FwaInventory.msisdn == activation.msisdn)
    )


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
