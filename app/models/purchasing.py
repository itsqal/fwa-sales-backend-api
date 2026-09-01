"""Modules G and I — the two purchase orders, and the shared replay cache.

Both POs have the same shape: a header carrying quantities and money, an append-only
status history, and no copy of any unit's state. Where a unit actually is comes from
``fwa_inventory`` and nowhere else — golden rule 6.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, updated_at_column
from app.models.admin import AdminUser
from app.models.commercial import Address, Brand, CallPlan
from app.models.enums import DevicePoStatus, MsisdnPoStatus
from app.models.inventory import DeviceModel
from app.models.organisation import DevicePartner, Mpx


class IdempotencyRecord(Base):
    """Replay cache for admin writes that create no single owning row.

    The AE side hangs an ``idempotency_key`` column on each aggregate table, which works
    because each of those writes creates exactly one row. Bulk supply inserts N
    inventory rows and pairing creates none at all, so the key needs a home of its own —
    together with the response it produced, because a retry must return the original
    answer rather than re-run the work to discover it already happened.
    """

    __tablename__ = "idempotency_record"
    __table_args__ = (
        UniqueConstraint("admin_user_id", "endpoint", "key", name="uq_idem_admin_endpoint_key"),
    )

    idempotency_record_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("admin_user.admin_user_id", ondelete="CASCADE"),
        nullable=False,
    )
    endpoint: Mapped[str] = mapped_column(String(120), nullable=False)
    key: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    # Detects the same key replayed with a different payload — a client bug, answered
    # with 409 rather than by silently returning the answer to a different question.
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status_code: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    created_at: Mapped[created_at_column]


class MsisdnPo(Base):
    """A Device Partner's request to IOH for numbers on a given call plan and brand."""

    __tablename__ = "msisdn_po"
    __table_args__ = (
        UniqueConstraint("po_code", name="uq_msisdn_po_code"),
        CheckConstraint(
            "status IN ('DIAJUKAN','DIPROSES','DITERIMA','DITOLAK','DIBATALKAN')",
            name="ck_msisdn_po_status",
        ),
        CheckConstraint("qty_requested > 0", name="ck_msisdn_po_qty"),
        CheckConstraint(
            "status <> 'DITOLAK' OR rejected_reason IS NOT NULL",
            name="ck_msisdn_po_rejected",
        ),
        Index("ix_msisdn_po_dp", "device_partner_id", text("submitted_at DESC")),
        Index("ix_msisdn_po_status", "status", text("submitted_at DESC")),
    )

    msisdn_po_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # {DP_CODE}-{YYYYMMDD}-{SEQ}. Server-generated; never accepted from a client.
    po_code: Mapped[str] = mapped_column(String(40), nullable=False)
    device_partner_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("device_partner.device_partner_id"), nullable=False
    )
    call_plan_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("call_plan.call_plan_id"), nullable=False
    )
    brand_code: Mapped[str] = mapped_column(String(10), ForeignKey("brand.code"), nullable=False)
    qty_requested: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[MsisdnPoStatus] = mapped_column(
        Enum(MsisdnPoStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
        server_default=text("'DIAJUKAN'"),
    )
    note: Mapped[str | None] = mapped_column(Text)
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    created_by_admin_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admin_user.admin_user_id"), nullable=False
    )
    supplied_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admin_user.admin_user_id")
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[created_at_column]
    updated_at: Mapped[updated_at_column]

    device_partner: Mapped[DevicePartner] = relationship(lazy="joined")
    call_plan: Mapped[CallPlan] = relationship(lazy="joined")
    brand: Mapped[Brand] = relationship(lazy="joined")
    created_by: Mapped[AdminUser] = relationship(lazy="joined", foreign_keys=[created_by_admin_id])


class MsisdnPoStatusHistory(Base):
    """Append-only. Written in the same transaction as the change it records."""

    __tablename__ = "msisdn_po_status_history"
    __table_args__ = (Index("ix_msisdn_po_hist", "msisdn_po_id", "changed_at"),)

    history_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    msisdn_po_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("msisdn_po.msisdn_po_id", ondelete="CASCADE"),
        nullable=False,
    )
    old_status: Mapped[str | None] = mapped_column(String(20))
    new_status: Mapped[str] = mapped_column(String(20), nullable=False)
    changed_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admin_user.admin_user_id")
    )
    note: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    changed_by: Mapped[AdminUser | None] = relationship(lazy="joined")


class DevicePo(Base):
    """An MPX order for devices from a Device Partner."""

    __tablename__ = "device_po"
    __table_args__ = (
        UniqueConstraint("po_code", name="uq_device_po_code"),
        CheckConstraint(
            "status IN ('DIAJUKAN','DIPROSES','DIKIRIM','PERIKSA','DITERIMA',"
            "'DITOLAK','DIBATALKAN')",
            name="ck_device_po_status",
        ),
        CheckConstraint("qty > 0", name="ck_device_po_qty"),
        CheckConstraint("unit_price_idr > 0", name="ck_device_po_price"),
        CheckConstraint("total_idr = unit_price_idr * qty", name="ck_device_po_total"),
        CheckConstraint(
            "pic_phone IS NULL OR pic_phone ~ '^(62|0)[0-9]{8,13}$'",
            name="ck_device_po_phone",
        ),
        CheckConstraint(
            "status <> 'DITOLAK' OR rejected_reason IS NOT NULL",
            name="ck_device_po_rejected",
        ),
        Index("ix_device_po_mpx", "mpx_id", text("submitted_at DESC")),
        Index("ix_device_po_dp", "device_partner_id", text("submitted_at DESC")),
        Index("ix_device_po_status", "status", text("submitted_at DESC")),
    )

    device_po_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    po_code: Mapped[str] = mapped_column(String(80), nullable=False)
    mpx_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("mpx.mpx_id"), nullable=False
    )
    device_partner_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("device_partner.device_partner_id"), nullable=False
    )
    device_model_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("device_model.device_model_id"), nullable=False
    )
    brand_code: Mapped[str] = mapped_column(String(10), ForeignKey("brand.code"), nullable=False)
    qty: Mapped[int] = mapped_column(nullable=False)
    # Snapshot, never a join. A price change must not rewrite a closed order.
    unit_price_idr: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_idr: Mapped[int] = mapped_column(BigInteger, nullable=False)
    address_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("address.address_id"), nullable=False
    )
    pic_name: Mapped[str | None] = mapped_column(String(150))
    pic_phone: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[DevicePoStatus] = mapped_column(
        Enum(DevicePoStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
        server_default=text("'DIAJUKAN'"),
    )
    created_by_admin_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admin_user.admin_user_id"), nullable=False
    )
    accepted_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admin_user.admin_user_id")
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[created_at_column]
    updated_at: Mapped[updated_at_column]

    mpx: Mapped[Mpx] = relationship(lazy="joined")
    device_partner: Mapped[DevicePartner] = relationship(lazy="joined")
    device_model: Mapped[DeviceModel] = relationship(lazy="joined")
    brand: Mapped[Brand] = relationship(lazy="joined")
    address: Mapped[Address] = relationship(lazy="joined")
    created_by: Mapped[AdminUser] = relationship(lazy="joined", foreign_keys=[created_by_admin_id])


class DevicePoStatusHistory(Base):
    """Backs the *Riwayat* panel. Append-only."""

    __tablename__ = "device_po_status_history"
    __table_args__ = (Index("ix_device_po_hist", "device_po_id", "changed_at"),)

    history_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    device_po_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("device_po.device_po_id", ondelete="CASCADE"),
        nullable=False,
    )
    old_status: Mapped[str | None] = mapped_column(String(20))
    new_status: Mapped[str] = mapped_column(String(20), nullable=False)
    changed_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admin_user.admin_user_id")
    )
    # Where the shipment step writes "J&T Express | JD0463672772".
    note: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    changed_by: Mapped[AdminUser | None] = relationship(lazy="joined")
