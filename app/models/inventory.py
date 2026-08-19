"""Module B — product and inventory master."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, updated_at_column
from app.models.enums import InventoryStatus


class DeviceModel(Base):
    """CPE catalogue. Surfaces read-only as "Tipe Modem" on the activation form."""

    __tablename__ = "device_model"
    __table_args__ = (UniqueConstraint("model_code", name="uq_device_model_code"),)

    device_model_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    model_code: Mapped[str] = mapped_column(String(60), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(60))
    sku: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[created_at_column]


class FwaInventory(Base):
    """The MSISDN-IMEI bundle produced by the Device Partner.

    This table is what makes "scan MSISDN → auto-fill IMEI + Tipe Modem" possible, and
    it is the reason the client is never trusted to assert a device: the server reads
    the pairing from here and snapshots it onto the activation.
    """

    __tablename__ = "fwa_inventory"
    __table_args__ = (
        CheckConstraint("msisdn ~ '^62[0-9]{8,13}$'", name="ck_inv_msisdn"),
        CheckConstraint("imei ~ '^[0-9]{14,16}$'", name="ck_inv_imei"),
        CheckConstraint("iccid IS NULL OR iccid ~ '^[0-9]{18,20}$'", name="ck_inv_iccid"),
        CheckConstraint(
            "status IN ('AVAILABLE','ALLOCATED','CONSUMED','ACTIVATED','RETURNED','BLOCKED')",
            name="ck_inv_status",
        ),
        UniqueConstraint("imei", name="uq_inv_imei"),
        UniqueConstraint("iccid", name="uq_inv_iccid"),
        Index("ix_inv_allocated_ae", "allocated_ae_id", "status"),
    )

    msisdn: Mapped[str] = mapped_column(String(15), primary_key=True)
    iccid: Mapped[str | None] = mapped_column(String(22))
    imei: Mapped[str] = mapped_column(String(16), nullable=False)
    device_model_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("device_model.device_model_id")
    )
    allocated_ae_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("account_executive.ae_id")
    )
    allocated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[InventoryStatus] = mapped_column(
        Enum(InventoryStatus, native_enum=False, create_constraint=False, length=20),
        nullable=False,
        server_default=text("'AVAILABLE'"),
    )
    created_at: Mapped[created_at_column]
    updated_at: Mapped[updated_at_column]

    device_model: Mapped[DeviceModel | None] = relationship(lazy="joined")
