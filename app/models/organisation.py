"""Module E — the two counterparty organisations in the supply chain.

A Device Partner supplies hardware; an MPX distributes it and allocates units to
Account Executives. Both exist so that an :class:`~app.models.admin.AdminUser` has
something to be bound to, and so that org scoping is a foreign key rather than a
string comparison.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column
from app.models.enums import MpxCircle, OrgStatus
from app.models.identity import Region


class DevicePartner(Base):
    """ADVAN, RABIT, ZTE, HKM, HUAWEI, BANGGA."""

    __tablename__ = "device_partner"
    __table_args__ = (
        UniqueConstraint("code", name="uq_device_partner_code"),
        CheckConstraint("code ~ '^[A-Z0-9]{2,20}$'", name="ck_device_partner_code"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_device_partner_status"),
    )

    device_partner_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Prefix of every MSISDN PO number this partner raises: ADVAN-20250523-207.
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    status: Mapped[OrgStatus] = mapped_column(
        Enum(OrgStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    created_at: Mapped[created_at_column]


class Mpx(Base):
    """Distributor / stock point.

    ``code`` is the canonical identifier and the one thing that is joined on:
    ``account_executive.mpx_code`` already carries this exact format, which is what
    lets an MPX admin list the AEs they may allocate to without a schema change to the
    live AE table. The three incompatible legacy identifier formats seen in the mockups
    live in ``external_ref``, never here.
    """

    __tablename__ = "mpx"
    __table_args__ = (
        UniqueConstraint("code", name="uq_mpx_code"),
        CheckConstraint("code ~ '^MPX-[A-Z]{2,4}-[0-9]{2}$'", name="ck_mpx_code"),
        CheckConstraint(
            "circle IS NULL OR circle IN ('JAVA','KALISUMAPA','SUMATERA')",
            name="ck_mpx_circle",
        ),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="ck_mpx_status"),
    )

    mpx_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    # Rendered in the MPX topbar, e.g. "PT Internet Rakyat Makmur".
    legal_name: Mapped[str | None] = mapped_column(String(200))
    circle: Mapped[MpxCircle | None] = mapped_column(
        Enum(MpxCircle, native_enum=False, create_constraint=False, length=20)
    )
    region_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("region.region_id")
    )
    external_ref: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[OrgStatus] = mapped_column(
        Enum(OrgStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
        server_default=text("'ACTIVE'"),
    )
    created_at: Mapped[created_at_column]

    region: Mapped[Region | None] = relationship(lazy="joined")
