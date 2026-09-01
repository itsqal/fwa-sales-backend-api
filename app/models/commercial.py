"""Module F — commercial master data for the supply chain.

Call plans and telco brands are the two reference lists every PO form reads. The
address book is MPX-scoped delivery data, which is why it lives here rather than with
the organisations: an address belongs to a stock point, not to the company.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, updated_at_column
from app.models.enums import CallPlanKind
from app.models.organisation import Mpx


class CallPlan(Base):
    """Commercial plan attached to an MSISDN request.

    ``quota_gb`` is nullable because *Saldo Mobo* is a balance top-up rather than a
    data bundle. ``ck_call_plan_quota`` ties the two columns together so that fact is
    enforced once in the database instead of being re-checked by every consumer.
    """

    __tablename__ = "call_plan"
    __table_args__ = (
        UniqueConstraint("code", name="uq_call_plan_code"),
        CheckConstraint("kind IN ('DATA','BALANCE')", name="ck_call_plan_kind"),
        CheckConstraint(
            "(kind = 'DATA'    AND quota_gb IS NOT NULL AND quota_gb > 0) OR "
            "(kind = 'BALANCE' AND quota_gb IS NULL)",
            name="ck_call_plan_quota",
        ),
    )

    call_plan_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    kind: Mapped[CallPlanKind] = mapped_column(
        Enum(CallPlanKind, native_enum=False, create_constraint=False, length=10),
        nullable=False,
    )
    quota_gb: Mapped[int | None] = mapped_column()
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("TRUE"))
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[created_at_column]


class Brand(Base):
    """Telco retail brand — ``IM3`` or ``3ID``.

    Natural-key primary key on purpose: there are exactly two rows, every referencing
    column is named ``brand_code``, and a surrogate UUID would make each PO join
    unreadable in exchange for nothing.

    Not to be confused with ``device_model.brand``, which is the hardware manufacturer.
    """

    __tablename__ = "brand"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(60), nullable=False)
    # The storefront name for the same brand: Gerai IM3, 3Store. The mockups used the
    # two interchangeably; holding both stops the drift.
    outlet_name: Mapped[str] = mapped_column(String(60), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("TRUE"))
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[created_at_column]


class Address(Base):
    """An entry in the MPX *Alamat Penerima* book.

    Every column here is rendered by the *Alamat Lengkap* modal. Coordinates and postal
    code are nullable because an address book entry is usable without them; recipient,
    line1, city and province are not, because a shipment cannot be delivered without
    them.
    """

    __tablename__ = "address"
    __table_args__ = (
        # Contact number, not an FWA MSISDN: stored as typed, 08xx or 62xx, exactly as
        # customer.phone_number is.
        CheckConstraint("recipient_phone ~ '^(62|0)[0-9]{8,13}$'", name="ck_address_phone"),
        CheckConstraint("latitude IS NULL OR latitude BETWEEN -90 AND 90", name="ck_address_lat"),
        CheckConstraint(
            "longitude IS NULL OR longitude BETWEEN -180 AND 180", name="ck_address_lng"
        ),
        CheckConstraint("(latitude IS NULL) = (longitude IS NULL)", name="ck_address_geo_pair"),
        Index("uq_address_one_default", "mpx_id", unique=True, postgresql_where=text("is_default")),
        Index("ix_address_mpx", "mpx_id", postgresql_where=text("is_active")),
    )

    address_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    mpx_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("mpx.mpx_id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(60), nullable=False)
    recipient_name: Mapped[str] = mapped_column(String(150), nullable=False)
    recipient_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    line1: Mapped[str] = mapped_column(Text, nullable=False)
    kelurahan: Mapped[str | None] = mapped_column(String(120))
    kecamatan: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    province: Mapped[str] = mapped_column(String(120), nullable=False)
    postal_code: Mapped[str | None] = mapped_column(String(10))
    latitude: Mapped[float | None] = mapped_column()
    longitude: Mapped[float | None] = mapped_column()
    gmaps_url: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(nullable=False, server_default=text("FALSE"))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("TRUE"))
    created_at: Mapped[created_at_column]
    updated_at: Mapped[updated_at_column]

    mpx: Mapped[Mpx] = relationship(lazy="joined")
