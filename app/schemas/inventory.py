"""Inventory response bodies. Backs the barcode scanner on the activation form."""

from __future__ import annotations

from pydantic import Field

from app.models.enums import IneligibilityReason, InventoryStatus, NetworkGeneration
from app.schemas.common import CamelModel


class DeviceModelOut(CamelModel):
    model_code: str = Field(examples=["HKM 127+"])
    brand: str | None = Field(default=None, examples=["HKM"])
    network_generation: NetworkGeneration | None = Field(
        default=None,
        description="Radio generation of the CPE. Absent when the model is uncategorised.",
    )


class InventoryItemOut(CamelModel):
    msisdn: str = Field(examples=["6285882724305"])
    iccid: str | None = Field(default=None, examples=["8962010000203921317"])
    imei: str = Field(examples=["355806671396654"])
    device_model: DeviceModelOut | None = None
    status: InventoryStatus
    eligible: bool = Field(description="Whether this AE may activate this MSISDN right now.")
    reason: IneligibilityReason | None = Field(
        default=None, description="Populated only when `eligible` is false."
    )
