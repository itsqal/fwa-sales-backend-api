"""Module C — the AE equivalent of "Sell In": a HiFi AIR unit handed to a customer."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, updated_at_column
from app.models.customer import Customer
from app.models.enums import ActivationStatus


class Activation(Base):
    """Created as NOT_ACTIVATED; flips to ACTIVATED when the Gross Add feed confirms.

    That gap is exactly the green *Activated* / red *Not Activated* badge on
    **Daftar Aktivasi**. ``imei`` and ``device_model_code`` are snapshots taken from
    inventory at submit time, so history survives later master-data edits.
    """

    __tablename__ = "activation"
    __table_args__ = (
        CheckConstraint(
            "status IN ('NOT_ACTIVATED','ACTIVATED','FAILED','CANCELLED')",
            name="ck_act_status",
        ),
        # An ACTIVATED row must carry a GA date. This is what stops any code path
        # other than the GA feed from turning a badge green.
        CheckConstraint(
            "status <> 'ACTIVATED' OR activation_date IS NOT NULL",
            name="ck_act_ga",
        ),
        UniqueConstraint("msisdn", name="uq_act_msisdn"),
        UniqueConstraint("idempotency_key", name="uq_act_idem"),
        Index("ix_act_ae_date", "ae_id", text("submitted_at DESC")),
        Index("ix_act_ae_status", "ae_id", "status", text("submitted_at DESC")),
        Index("ix_act_customer", "customer_id"),
    )

    activation_id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True, start=2900000000), primary_key=True
    )
    ae_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("account_executive.ae_id"), nullable=False
    )
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customer.customer_id"), nullable=False
    )
    msisdn: Mapped[str] = mapped_column(
        String(15), ForeignKey("fwa_inventory.msisdn"), nullable=False
    )
    imei: Mapped[str] = mapped_column(String(16), nullable=False)
    device_model_code: Mapped[str | None] = mapped_column(String(60))
    latitude: Mapped[float] = mapped_column(nullable=False)
    longitude: Mapped[float] = mapped_column(nullable=False)
    geo_accuracy_m: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    geo_verified: Mapped[bool] = mapped_column(nullable=False, server_default=text("FALSE"))
    is_mocked: Mapped[bool] = mapped_column(nullable=False, server_default=text("FALSE"))
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    status: Mapped[ActivationStatus] = mapped_column(
        Enum(ActivationStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
        server_default=text("'NOT_ACTIVATED'"),
    )
    # GA Date. Never written by an AE-facing code path — only by the GCP feed.
    activation_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ga_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    created_at: Mapped[created_at_column]
    updated_at: Mapped[updated_at_column]

    customer: Mapped[Customer] = relationship(lazy="joined")
