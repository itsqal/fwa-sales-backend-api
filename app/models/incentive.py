"""Module E — targets and incentive.

Nothing in this module invents a payout. ``incentive_rule`` rows are data supplied by
the business; the accrual service reads them. With no rules configured the "Insentif"
tile reads zero, which is the honest answer while the rules are undecided
(CLAUDE.md §11.1).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column
from app.models.enums import IncentiveEventType, LedgerStatus


class AeDailyTarget(Base):
    """Supplies the grey benchmark bars behind the pink actual bars on the home chart."""

    __tablename__ = "ae_daily_target"
    __table_args__ = (
        UniqueConstraint("ae_id", "target_date", name="uq_target_ae_date"),
        CheckConstraint("target_activations >= 0", name="ck_target_nonneg"),
    )

    target_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    ae_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("account_executive.ae_id"), nullable=False
    )
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    target_activations: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    target_new_customers: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[created_at_column]


class IncentiveRule(Base):
    __tablename__ = "incentive_rule"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('ACTIVATION','NEW_CUSTOMER','HOT_LEAD')", name="ck_rule_event"
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="ck_rule_dates"
        ),
    )

    rule_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    rule_name: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[IncentiveEventType] = mapped_column(
        Enum(IncentiveEventType, native_enum=False, create_constraint=False, length=30),
        nullable=False,
    )
    amount_idr: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # The condition language is not specified yet. The accrual service applies only
    # rules whose conditions are empty, rather than guessing at a semantics.
    conditions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[created_at_column]


class IncentiveLedger(Base):
    """Append-only accrual of AE earnings.

    The "Insentif" tile is ``SUM(amount_idr)`` over the selected period. The unique
    constraint on (rule, activation, customer) is what makes a replayed GA event safe:
    the second insert is rejected by the database rather than by application logic.
    """

    __tablename__ = "incentive_ledger"
    __table_args__ = (
        CheckConstraint("status IN ('ACCRUED','APPROVED','PAID','VOID')", name="ck_ledger_status"),
        UniqueConstraint("rule_id", "activation_id", "customer_id", name="uq_ledger_event"),
        Index("ix_ledger_ae_period", "ae_id", "period_ym"),
    )

    ledger_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    ae_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("account_executive.ae_id"), nullable=False
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("incentive_rule.rule_id")
    )
    activation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("activation.activation_id")
    )
    customer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("customer.customer_id"))
    amount_idr: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    earned_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_ym: Mapped[str] = mapped_column(CHAR(7), nullable=False)
    status: Mapped[LedgerStatus] = mapped_column(
        Enum(LedgerStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
        server_default=text("'ACCRUED'"),
    )
    created_at: Mapped[created_at_column]

    rule: Mapped[IncentiveRule | None] = relationship(lazy="joined")
