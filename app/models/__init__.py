"""SQLAlchemy models. One module per aggregate; the shape of record is docs/schema.sql."""

from app.models.activation import Activation
from app.models.attendance import Attendance
from app.models.customer import Customer, CustomerStatusHistory
from app.models.enums import (
    ActivationStatus,
    AeStatus,
    ApprovalStatus,
    CustomerStatus,
    IncentiveEventType,
    InventoryStatus,
    LedgerStatus,
)
from app.models.identity import AccountExecutive, AuthRefreshToken, Region
from app.models.incentive import AeDailyTarget, IncentiveLedger, IncentiveRule
from app.models.inventory import DeviceModel, FwaInventory

__all__ = [
    "AccountExecutive",
    "Activation",
    "ActivationStatus",
    "AeDailyTarget",
    "AeStatus",
    "ApprovalStatus",
    "Attendance",
    "AuthRefreshToken",
    "Customer",
    "CustomerStatus",
    "CustomerStatusHistory",
    "DeviceModel",
    "FwaInventory",
    "IncentiveEventType",
    "IncentiveLedger",
    "IncentiveRule",
    "InventoryStatus",
    "LedgerStatus",
    "Region",
]
