"""Modules I(b) and J — getting the devices there, and handing them to a salesman.

The last two tables in the chain. ``stock_allocation`` is the one that matters most:
its write is what makes a unit visible to the AE mobile app.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_column, updated_at_column
from app.models.admin import AdminUser
from app.models.enums import AllocationMode, ShipmentMilestoneType


class Shipment(Base):
    """One dispatch of a device PO.

    Unique per order, which holds precisely because reshipping is handled outside this
    system: an order goes out once, and a short delivery is corrected off-system before
    the MPX confirms receipt.
    """

    __tablename__ = "shipment"
    __table_args__ = (
        UniqueConstraint("device_po_id", name="uq_shipment_device_po"),
        CheckConstraint("length(btrim(awb)) > 0", name="ck_shipment_awb"),
        CheckConstraint("length(btrim(courier_name)) > 0", name="ck_shipment_courier"),
        CheckConstraint(
            "estimated_delivery_date IS NULL OR estimated_delivery_date >= shipped_at::date",
            name="ck_shipment_eta",
        ),
    )

    shipment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    device_po_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("device_po.device_po_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Free text by decision: the business asked to keep couriers manual for v1.
    courier_name: Mapped[str] = mapped_column(String(60), nullable=False)
    # Nomor Resi — the proof of dispatch named in the business process.
    awb: Mapped[str] = mapped_column(String(60), nullable=False)
    shipped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    estimated_delivery_date: Mapped[date | None] = mapped_column(Date)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admin_user.admin_user_id")
    )
    created_at: Mapped[created_at_column]
    updated_at: Mapped[updated_at_column]


class ShipmentMilestone(Base):
    """One step of the four-step Delivery Progress tracker."""

    __tablename__ = "shipment_milestone"
    __table_args__ = (
        CheckConstraint(
            "milestone IN ('SHIPPED','IN_TRANSIT','OUT_FOR_DELIVERY','DELIVERED')",
            name="ck_milestone",
        ),
        # Each step appears once, or the tracker draws a bar that goes backwards.
        UniqueConstraint("shipment_id", "milestone", name="uq_milestone_per_shipment"),
        Index("ix_milestone_shipment", "shipment_id", "occurred_at"),
    )

    milestone_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    shipment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("shipment.shipment_id", ondelete="CASCADE"),
        nullable=False,
    )
    milestone: Mapped[ShipmentMilestoneType] = mapped_column(
        Enum(ShipmentMilestoneType, native_enum=False, create_constraint=False, length=20),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admin_user.admin_user_id")
    )


class GoodsReceipt(Base):
    """MPX confirming a complete delivery.

    There is no line table and no partial flag. Confirmed 2026-09-01: a receipt can only
    be confirmed when every unit on the order is physically present, so which units were
    received is exactly the ``fwa_inventory`` rows carrying this order id — duplicating
    that into a PO-side table is what golden rule 6 forbids.
    """

    __tablename__ = "goods_receipt"
    __table_args__ = (
        UniqueConstraint("device_po_id", name="uq_goods_receipt_device_po"),
        CheckConstraint("qty_received > 0", name="ck_goods_receipt_qty"),
    )

    goods_receipt_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    device_po_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("device_po.device_po_id", ondelete="CASCADE"),
        nullable=False,
    )
    received_by_admin_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admin_user.admin_user_id"), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    qty_received: Mapped[int] = mapped_column(nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[created_at_column]

    received_by: Mapped[AdminUser] = relationship(lazy="joined")


class StockAllocation(Base):
    """One handover of stock from an MPX to an Account Executive.

    The write that makes a unit visible to the mobile app.
    """

    __tablename__ = "stock_allocation"
    __table_args__ = (
        CheckConstraint("mode IN ('AUTO','MANUAL')", name="ck_allocation_mode"),
        CheckConstraint("qty > 0", name="ck_allocation_qty"),
        Index("ix_allocation_mpx", "mpx_id", text("allocated_at DESC")),
        Index("ix_allocation_ae", "ae_id", text("allocated_at DESC")),
    )

    stock_allocation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    mpx_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("mpx.mpx_id"), nullable=False
    )
    ae_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("account_executive.ae_id"), nullable=False
    )
    allocated_by_admin_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admin_user.admin_user_id"), nullable=False
    )
    allocated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    mode: Mapped[AllocationMode] = mapped_column(
        Enum(AllocationMode, native_enum=False, create_constraint=False, length=10),
        nullable=False,
    )
    qty: Mapped[int] = mapped_column(nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[created_at_column]


class StockAllocationItem(Base):
    """The units in one allocation. Unique on ``msisdn``: a unit is allocated once."""

    __tablename__ = "stock_allocation_item"
    __table_args__ = (UniqueConstraint("msisdn", name="uq_allocation_item_msisdn"),)

    stock_allocation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("stock_allocation.stock_allocation_id", ondelete="CASCADE"),
        primary_key=True,
    )
    msisdn: Mapped[str] = mapped_column(
        String(15), ForeignKey("fwa_inventory.msisdn"), primary_key=True
    )
