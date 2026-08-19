"""Module C — field activity: the prospect an AE registers door-to-door."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_column, updated_at_column
from app.models.enums import CustomerStatus


class Customer(Base):
    """A prospect registered during door-to-door activity. One row per lead.

    The 10-digit ``customer_id`` is the "ID Customer" the UI shows in the activation
    dropdown, which is why the identity sequence starts at 1000000000 rather than 1.
    """

    __tablename__ = "customer"
    __table_args__ = (
        CheckConstraint("status IN ('EDUKASI','PURCHASE','HOT_LEADS')", name="ck_cust_status"),
        CheckConstraint("phone_number ~ '^(62|0)[0-9]{8,13}$'", name="ck_cust_phone"),
        CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_cust_lat"),
        CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_cust_lng"),
        UniqueConstraint("idempotency_key", name="uq_cust_idem"),
        Index("ix_cust_ae_date", "ae_id", text("visit_date DESC")),
        Index("ix_cust_ae_status", "ae_id", "status", text("visit_date DESC")),
        Index("ix_cust_geo", "latitude", "longitude"),
        # Guards against the same lead being registered twice by one AE.
        Index("uq_cust_ae_phone", "ae_id", "phone_number", unique=True),
    )

    customer_id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True, start=1000000000), primary_key=True
    )
    ae_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("account_executive.ae_id"), nullable=False
    )
    visit_date: Mapped[date] = mapped_column(Date, nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    latitude: Mapped[float] = mapped_column(nullable=False)
    longitude: Mapped[float] = mapped_column(nullable=False)
    geo_accuracy_m: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    geo_verified: Mapped[bool] = mapped_column(nullable=False, server_default=text("FALSE"))
    is_mocked: Mapped[bool] = mapped_column(nullable=False, server_default=text("FALSE"))
    resolved_address: Mapped[str | None] = mapped_column(Text)
    status: Mapped[CustomerStatus] = mapped_column(
        Enum(CustomerStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
    )
    visited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    idempotency_key: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    created_at: Mapped[created_at_column]
    updated_at: Mapped[updated_at_column]


class CustomerStatusHistory(Base):
    """Makes the funnel measurable: Hot Leads → Purchase conversion over time."""

    __tablename__ = "customer_status_history"
    __table_args__ = (
        CheckConstraint("new_status IN ('EDUKASI','PURCHASE','HOT_LEADS')", name="ck_hist_new"),
        Index("ix_hist_customer", "customer_id", text("changed_at DESC")),
    )

    history_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("customer.customer_id", ondelete="CASCADE"),
        nullable=False,
    )
    old_status: Mapped[CustomerStatus | None] = mapped_column(
        Enum(CustomerStatus, native_enum=False, create_constraint=False, length=20)
    )
    new_status: Mapped[CustomerStatus] = mapped_column(
        Enum(CustomerStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
    )
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("account_executive.ae_id")
    )
    note: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
