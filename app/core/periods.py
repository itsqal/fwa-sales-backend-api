"""Relative report windows (``7d`` / ``30d`` / ``mtd`` / ``ytd``) to concrete dates.

The window is resolved against the *server's* idea of today in the business timezone.
A phone with a wrong clock cannot shift its own reporting period.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from app.core.config import Settings
from app.core.errors import ValidationFailedError


class Period(StrEnum):
    LAST_7_DAYS = "7d"
    LAST_30_DAYS = "30d"
    MONTH_TO_DATE = "mtd"
    YEAR_TO_DATE = "ytd"


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date


def today(settings: Settings) -> date:
    return datetime.now(settings.tz).date()


def now(settings: Settings) -> datetime:
    return datetime.now(settings.tz)


def validate_optional_range(date_from: date | None, date_to: date | None) -> None:
    """Ordering check for endpoints that take a bare ``from``/``to`` pair.

    Both dates are individually valid, so nothing rejects a reversed pair until they
    are compared. Attendance history has no ``period`` parameter and so does not go
    through :func:`resolve_range`, but it owes the client the same answer.
    """
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValidationFailedError(
            "The 'from' date must not be after the 'to' date.",
            code="INVALID_DATE_RANGE",
            details=[{"field": "from", "issue": "must be on or before 'to'"}],
        )


def resolve_range(
    settings: Settings,
    *,
    period: Period | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> DateRange:
    """An explicit ``from``/``to`` pair overrides ``period``, per the contract."""
    current = today(settings)

    if date_from is not None or date_to is not None:
        start = date_from if date_from is not None else current
        end = date_to if date_to is not None else current
        if start > end:
            raise ValidationFailedError(
                "The 'from' date must not be after the 'to' date.",
                code="INVALID_DATE_RANGE",
                details=[{"field": "from", "issue": "must be on or before 'to'"}],
            )
        return DateRange(start=start, end=end)

    match period or Period.LAST_7_DAYS:
        case Period.LAST_7_DAYS:
            # Inclusive of today, so "7 Hari Terakhir" renders seven bars.
            return DateRange(start=current.fromordinal(current.toordinal() - 6), end=current)
        case Period.LAST_30_DAYS:
            return DateRange(start=current.fromordinal(current.toordinal() - 29), end=current)
        case Period.MONTH_TO_DATE:
            return DateRange(start=current.replace(day=1), end=current)
        case Period.YEAR_TO_DATE:
            return DateRange(start=current.replace(month=1, day=1), end=current)
