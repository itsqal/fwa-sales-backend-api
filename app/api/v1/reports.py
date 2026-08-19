"""Report endpoints. Backs the Home tiles and chart, and the Report screen."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.deps import AppSettings, CurrentAe, DbSession
from app.core.pagination import Pagination, get_pagination
from app.core.periods import Period, resolve_range
from app.schemas.common import Paginated, Wrapped
from app.schemas.report import DailyActivity, IncentiveEntry, ReportSummary
from app.services import reports as report_service

router = APIRouter(prefix="/reports", tags=["Reports"])


@router.get(
    "/summary",
    summary="Headline counters",
    response_model=Wrapped[ReportSummary],
)
async def get_report_summary(
    ae: CurrentAe,
    session: DbSession,
    settings: AppSettings,
    period: Annotated[Period, Query()] = Period.LAST_7_DAYS,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> Wrapped[ReportSummary]:
    """Backs the four tiles on **Report** and the two on **Home**.

    `incentiveIdr` is whole rupiah. The UI divides by 100 000 to render
    "242 Ratus Ribu Rupiah" — that scaling is presentation-only.
    """
    window = resolve_range(settings, period=period, date_from=date_from, date_to=date_to)
    return Wrapped[ReportSummary](
        data=await report_service.summary(session, ae_id=ae.ae_id, window=window)
    )


@router.get(
    "/daily-activity",
    summary="Daily series for the home-screen chart",
    response_model=Wrapped[list[DailyActivity]],
)
async def get_daily_activity(
    ae: CurrentAe,
    session: DbSession,
    settings: AppSettings,
    period: Annotated[Period, Query()] = Period.LAST_7_DAYS,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> Wrapped[list[DailyActivity]]:
    """One entry per day in range, including days with zero activity so the chart keeps
    an unbroken axis. `target` supplies the grey benchmark bars."""
    window = resolve_range(settings, period=period, date_from=date_from, date_to=date_to)
    return Wrapped[list[DailyActivity]](
        data=await report_service.daily_activity(session, ae_id=ae.ae_id, window=window)
    )


@router.get(
    "/incentives",
    summary="Incentive ledger for this AE",
    response_model=Paginated[IncentiveEntry],
)
async def list_incentives(
    ae: CurrentAe,
    session: DbSession,
    pagination: Annotated[Pagination, Depends(get_pagination)],
    period_ym: Annotated[
        str | None, Query(alias="periodYm", pattern=r"^[0-9]{4}-[0-9]{2}$")
    ] = None,
) -> Paginated[IncentiveEntry]:
    items, total = await report_service.list_incentives(
        session, ae_id=ae.ae_id, period_ym=period_ym, pagination=pagination
    )
    return Paginated[IncentiveEntry].model_validate(pagination.envelope(items, total))
