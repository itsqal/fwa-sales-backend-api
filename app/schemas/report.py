"""Report response bodies. Backs the Home tiles, the chart, and the Report screen."""

from __future__ import annotations

from datetime import date

from pydantic import Field

from app.models.enums import LedgerStatus
from app.schemas.common import CamelModel, nullable_field


class ReportSummary(CamelModel):
    period_from: date
    period_to: date
    total_activations: int = Field(description='"Total Aktivasi" tile.', examples=[120])
    activated_count: int = Field(description="Subset confirmed live by the GA feed.")
    not_activated_count: int
    new_customers: int = Field(description='"New Customer" tile.', examples=[118])
    hot_leads: int = Field(description='"Hot Leads" tile.', examples=[90])
    incentive_idr: int = Field(
        description=(
            '"Insentif" tile, in whole rupiah. The UI divides by 100 000 to render '
            '"242 Ratus Ribu Rupiah" — that scaling is presentation-only.'
        ),
        examples=[24200000],
    )
    conversion_rate: float = Field(description="activatedCount / newCustomers, 0–1.")


class DailyActivity(CamelModel):
    date: date
    activations: int = Field(description="Pink bar.")
    target: int = Field(description="Grey benchmark bar.")
    new_customers: int
    hot_leads: int


class IncentiveEntry(CamelModel):
    ledger_id: int
    # Declared oneOf [int64, null]: a ledger entry may be a manual adjustment rather
    # than an activation payout, and the client shows those differently.
    activation_id: int | None = nullable_field(default=None)
    rule_name: str = Field(examples=["Activation bonus — Aug 2026"])
    amount_idr: int = Field(examples=[50000])
    earned_date: date
    period_ym: str = Field(examples=["2026-08"])
    status: LedgerStatus
