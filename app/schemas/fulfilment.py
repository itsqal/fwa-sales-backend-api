"""Shipment, goods receipt, stock and allocation bodies."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field

from app.models.enums import AllocationMode, DevicePoStatus, ShipmentMilestoneType
from app.schemas.common import CamelModel, nullable_field

# ---------------------------------------------------------------------------
# Shipment
# ---------------------------------------------------------------------------


class CreateShipmentRequest(CamelModel):
    """The screen the UI review found missing entirely (issue #1).

    `courierName` is free text rather than a code: the business asked to keep couriers
    manual for v1, so there is no courier reference table to validate against.
    """

    courier_name: str = Field(min_length=1, max_length=60, examples=["J&T Express"])
    awb: str = Field(
        min_length=1,
        max_length=60,
        description="Nomor Resi — the proof of dispatch.",
        examples=["JD0463672772"],
    )
    estimated_delivery_date: date | None = None
    note: str | None = Field(default=None, max_length=500)


class MilestoneRequest(CamelModel):
    milestone: ShipmentMilestoneType
    occurred_at: datetime | None = Field(
        default=None, description="Defaults to now. The server remains authoritative on time."
    )
    note: str | None = Field(default=None, max_length=500)


class MilestoneOut(CamelModel):
    milestone: ShipmentMilestoneType
    occurred_at: datetime
    note: str | None = None


class ShipmentOut(CamelModel):
    """Backs the Delivery Progress tracker."""

    shipment_id: uuid.UUID
    device_po_id: uuid.UUID
    courier_name: str
    awb: str
    shipped_at: datetime
    estimated_delivery_date: date | None = None
    delivered_at: datetime | None = None
    milestones: list[MilestoneOut]


# ---------------------------------------------------------------------------
# Goods receipt
# ---------------------------------------------------------------------------


class ReceiptRequest(CamelModel):
    """One final confirmation.

    There is no partial option and no per-unit condition. Confirmed 2026-09-01: if the
    box is short, the reshipping happens outside this system and the MPX confirms only
    once the full list is physically present.
    """

    confirmed: bool = Field(
        description=(
            "The attestation. Must be true — it names a person in the UI and is what "
            "the status-history *oleh* column records."
        )
    )
    note: str | None = Field(default=None, max_length=500)


class ReceiptOut(CamelModel):
    goods_receipt_id: uuid.UUID
    device_po_id: uuid.UUID
    status: DevicePoStatus
    qty_received: int
    received_at: datetime
    received_by: str | None = None
    note: str | None = None


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------


class StockLineOut(CamelModel):
    """One row of the *Stok Tersedia* table.

    The three columns are defined here because the mockups were arithmetically
    impossible (issue #5 showed available 20 + allocated 21 = total 10). Activated
    units have left the MPX entirely and appear in none of them.
    """

    device_model_id: uuid.UUID | None = None
    device_model_code: str | None = None
    available: int = Field(description="RECEIVED and not yet allocated — *Jml. Tersedia*.")
    allocated: int = Field(description="ALLOCATED and not yet activated — *Jml. Dialokasikan*.")
    total: int = Field(description="available + allocated: everything this MPX still holds.")


class StockBundleOut(CamelModel):
    """One allocatable unit. Backs the Manual picker, ordered oldest-received first."""

    msisdn: str
    imei: str | None = nullable_field(default=None)
    device_model_code: str | None = None
    brand_code: str | None = None
    # The *Tgl. Terima* column, and the key AUTO allocation orders by.
    received_at: datetime | None = None


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------


class AllocationLine(CamelModel):
    device_model_id: uuid.UUID
    qty: int = Field(gt=0, le=100_000)


class CreateAllocationRequest(CamelModel):
    """Allocate stock to one Account Executive.

    `AUTO` takes the oldest received units for each requested model. `MANUAL` names the
    units outright. Exactly one of `items` or `msisdns` is supplied, matching the mode.
    """

    ae_id: uuid.UUID = Field(
        description=(
            "The salesman receiving the stock. This is an admin-audience endpoint, so "
            "naming another principal is the point — the caller is still confined to "
            "its own MPX by the token."
        )
    )
    mode: AllocationMode
    items: list[AllocationLine] | None = Field(
        default=None, description="AUTO mode: how many of each device model."
    )
    msisdns: list[str] | None = Field(default=None, description="MANUAL mode: the exact units.")
    note: str | None = Field(default=None, max_length=500)


class AllocationOut(CamelModel):
    stock_allocation_id: uuid.UUID
    ae_id: uuid.UUID
    ae_code: str | None = None
    mode: AllocationMode
    qty: int
    allocated_at: datetime
    allocated_by: str | None = None
    note: str | None = None


class AllocationDetailOut(AllocationOut):
    msisdns: list[str]
