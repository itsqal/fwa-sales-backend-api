"""Home dashboard and the Report screen."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import Pagination
from app.core.periods import DateRange
from app.models.activation import Activation
from app.models.customer import Customer
from app.models.enums import ActivationStatus, CustomerStatus
from app.models.incentive import IncentiveLedger, IncentiveRule
from app.schemas.report import DailyActivity, IncentiveEntry, ReportSummary
from app.services.incentives import sum_for_period, to_whole_rupiah


async def summary(session: AsyncSession, *, ae_id: uuid.UUID, window: DateRange) -> ReportSummary:
    submitted_on = func.date(Activation.submitted_at)
    activation_filters = [
        Activation.ae_id == ae_id,
        submitted_on >= window.start,
        submitted_on <= window.end,
    ]

    counts = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Activation.status == ActivationStatus.ACTIVATED),
                func.count().filter(Activation.status == ActivationStatus.NOT_ACTIVATED),
            ).where(*activation_filters)
        )
    ).one()
    total_activations, activated, not_activated = (int(value) for value in counts)

    customer_filters = [
        Customer.ae_id == ae_id,
        Customer.visit_date >= window.start,
        Customer.visit_date <= window.end,
    ]
    customer_counts = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Customer.status == CustomerStatus.HOT_LEADS),
            ).where(*customer_filters)
        )
    ).one()
    new_customers, hot_leads = (int(value) for value in customer_counts)

    incentive_idr = await sum_for_period(session, ae_id=ae_id, start=window.start, end=window.end)

    return ReportSummary(
        period_from=window.start,
        period_to=window.end,
        total_activations=total_activations,
        activated_count=activated,
        not_activated_count=not_activated,
        new_customers=new_customers,
        hot_leads=hot_leads,
        incentive_idr=incentive_idr,
        conversion_rate=round(activated / new_customers, 4) if new_customers else 0.0,
    )


async def daily_activity(
    session: AsyncSession, *, ae_id: uuid.UUID, window: DateRange
) -> list[DailyActivity]:
    """One row per day in range, including zero-activity days.

    The date spine is generated inside ``fn_ae_daily_activity`` rather than derived from
    the rows that happen to exist, so the chart keeps an unbroken axis across a quiet
    week instead of silently collapsing it.
    """
    rows = await session.execute(
        text(
            "SELECT activity_date, activations, target_activations, new_customers, hot_leads "
            "FROM fn_ae_daily_activity(:ae_id, :from_date, :to_date)"
        ),
        {"ae_id": str(ae_id), "from_date": window.start, "to_date": window.end},
    )
    return [
        DailyActivity(
            date=activity_date,
            activations=int(activations),
            target=int(target),
            new_customers=int(new_customers),
            hot_leads=int(hot_leads),
        )
        for activity_date, activations, target, new_customers, hot_leads in rows.all()
    ]


async def list_incentives(
    session: AsyncSession,
    *,
    ae_id: uuid.UUID,
    period_ym: str | None,
    pagination: Pagination,
) -> tuple[list[IncentiveEntry], int]:
    filters = [IncentiveLedger.ae_id == ae_id]
    if period_ym:
        filters.append(IncentiveLedger.period_ym == period_ym)

    total = await session.scalar(select(func.count()).select_from(IncentiveLedger).where(*filters))

    rows = await session.execute(
        select(IncentiveLedger, IncentiveRule.rule_name)
        .outerjoin(IncentiveRule, IncentiveRule.rule_id == IncentiveLedger.rule_id)
        .where(*filters)
        .order_by(IncentiveLedger.earned_date.desc(), IncentiveLedger.ledger_id.desc())
        .limit(pagination.limit)
        .offset(pagination.offset)
    )

    return [
        IncentiveEntry(
            ledger_id=entry.ledger_id,
            activation_id=entry.activation_id,
            rule_name=rule_name or "Manual adjustment",
            amount_idr=to_whole_rupiah(entry.amount_idr),
            earned_date=entry.earned_date,
            period_ym=entry.period_ym.strip(),
            status=entry.status,
        )
        for entry, rule_name in rows.all()
    ], int(total or 0)
