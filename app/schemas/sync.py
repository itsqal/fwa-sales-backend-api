"""Offline bootstrap payload for the mobile client."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.auth import AccountExecutiveOut
from app.schemas.common import CamelModel
from app.schemas.customer import CustomerOut
from app.schemas.inventory import InventoryItemOut


class BootstrapResponse(CamelModel):
    server_time: datetime = Field(
        description="Authoritative server clock, for drift correction on the device."
    )
    profile: AccountExecutiveOut
    inventory: list[InventoryItemOut]
    customers: list[CustomerOut]
