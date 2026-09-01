"""Module B — product and inventory master."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, updated_at_column
from app.models.enums import InventoryStatus, NetworkGeneration


class DeviceModel(Base):
    """CPE catalogue. Surfaces read-only as "Tipe Modem" on the activation form."""

    __tablename__ = "device_model"
    __table_args__ = (
        UniqueConstraint("model_code", name="uq_device_model_code"),
        CheckConstraint(
            "network_generation IS NULL OR network_generation IN ('4G','5G')",
            name="ck_device_model_netgen",
        ),
        CheckConstraint(
            "list_price_idr IS NULL OR list_price_idr > 0", name="ck_device_model_price"
        ),
    )

    device_model_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    model_code: Mapped[str] = mapped_column(String(60), nullable=False)
    # The hardware manufacturer as free text (HKM, RABIT, ADVAN), published to the
    # mobile app as DeviceModelOut.brand and therefore never renamed. NOT the telco
    # brand — that is `brand_code`, and it points at the `brand` table.
    brand: Mapped[str | None] = mapped_column(String(60))
    # The structured form of the same fact, backfilled from `brand` in migration 0004.
    device_partner_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("device_partner.device_partner_id")
    )
    sku: Mapped[str | None] = mapped_column(String(60))
    # Selects the incentive tier on a confirmed Gross Add. Nullable on purpose: a model
    # nobody has categorised yet accrues nothing and logs a warning, rather than being
    # defaulted into a tier and quietly paying the wrong amount.
    network_generation: Mapped[NetworkGeneration | None] = mapped_column(
        Enum(
            NetworkGeneration,
            native_enum=False,
            create_constraint=False,
            length=10,
            # Without this SQLAlchemy would store the member *names*, and the CHECK
            # constraint would reject every row.
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        )
    )
    # Catalogue price in whole rupiah. Nullable on the same reasoning as
    # network_generation: no confirmed price list exists, and a model without one must
    # fail to be ordered rather than be ordered at a guessed price.
    list_price_idr: Mapped[int | None] = mapped_column(BigInteger)
    image_url: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("TRUE"))
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
            "status IN ('AVAILABLE','ALLOCATED','CONSUMED','ACTIVATED','RETURNED','BLOCKED',"
            "'MSISDN_ISSUED','PAIRED','ASSIGNED','SHIPPED','RECEIVED')",
            name="ck_inv_status",
        ),
        # An unpaired number may exist, but only in the one state that precedes pairing.
        # This is what stops a NULL imei ever reaching a status the AE app renders.
        CheckConstraint(
            "status = 'MSISDN_ISSUED' OR imei IS NOT NULL",
            name="imei_required_once_paired",
        ),
        UniqueConstraint("imei", name="uq_inv_imei"),
        UniqueConstraint("iccid", name="uq_inv_iccid"),
        Index("ix_inv_allocated_ae", "allocated_ae_id", "status"),
        Index(
            "ix_inv_msisdn_po",
            "msisdn_po_id",
            postgresql_where=text("msisdn_po_id IS NOT NULL"),
        ),
        Index(
            "ix_inv_device_po",
            "device_po_id",
            postgresql_where=text("device_po_id IS NOT NULL"),
        ),
        Index("ix_inv_mpx_status", "mpx_id", "status"),
        Index(
            "ix_inv_allocatable",
            "mpx_id",
            "received_at",
            "msisdn",
            postgresql_where=text("status = 'RECEIVED'"),
        ),
    )

    msisdn: Mapped[str] = mapped_column(String(15), primary_key=True)
    iccid: Mapped[str | None] = mapped_column(String(22))
    # Nullable since migration 0005: a number IOH has supplied has no IMEI until the
    # Device Partner pairs one to it. `imei_required_once_paired` confines that to the
    # single pre-pairing state.
    imei: Mapped[str | None] = mapped_column(String(16))
    device_model_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("device_model.device_model_id")
    )
    # --- Supply chain lifecycle, added by migration 0005 ---
    # No ForeignKey on these two: msisdn_po and device_po arrive in migrations 0006 and
    # 0008, which add the constraints.
    msisdn_po_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    device_po_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    mpx_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("mpx.mpx_id"))
    brand_code: Mapped[str | None] = mapped_column(String(10), ForeignKey("brand.code"))
    call_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("call_plan.call_plan_id")
    )
    paired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paired_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admin_user.admin_user_id")
    )
    # The FIFO key for automatic allocation, which is why it is stored and not derived.
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
