"""SQLAlchemy models. One module per aggregate; the shape of record is docs/schema.sql."""

from app.models.activation import Activation
from app.models.admin import AdminRefreshToken, AdminUser
from app.models.attendance import Attendance
from app.models.commercial import Address, Brand, CallPlan
from app.models.customer import Customer, CustomerStatusHistory
from app.models.enums import (
    ActivationStatus,
    AdminRole,
    AdminStatus,
    AeStatus,
    AllocationMode,
    ApprovalStatus,
    BrandScope,
    CallPlanKind,
    CustomerStatus,
    DevicePoStatus,
    IncentiveEventType,
    InventoryStatus,
    LedgerStatus,
    MpxCircle,
    MsisdnPoStatus,
    NetworkGeneration,
    OrgStatus,
    ShipmentMilestoneType,
)
from app.models.fulfilment import (
    GoodsReceipt,
    Shipment,
    ShipmentMilestone,
    StockAllocation,
    StockAllocationItem,
)
from app.models.identity import AccountExecutive, AuthRefreshToken, Region
from app.models.incentive import AeDailyTarget, IncentiveLedger, IncentiveRule
from app.models.inventory import DeviceModel, FwaInventory
from app.models.organisation import DevicePartner, Mpx
from app.models.purchasing import (
    DevicePo,
    DevicePoStatusHistory,
    IdempotencyRecord,
    MsisdnPo,
    MsisdnPoStatusHistory,
)

__all__ = [
    "AccountExecutive",
    "Activation",
    "ActivationStatus",
    "Address",
    "AdminRefreshToken",
    "AdminRole",
    "AdminStatus",
    "AdminUser",
    "AeDailyTarget",
    "AeStatus",
    "AllocationMode",
    "ApprovalStatus",
    "Attendance",
    "AuthRefreshToken",
    "Brand",
    "BrandScope",
    "CallPlan",
    "CallPlanKind",
    "Customer",
    "CustomerStatus",
    "CustomerStatusHistory",
    "DeviceModel",
    "DevicePartner",
    "DevicePo",
    "DevicePoStatus",
    "DevicePoStatusHistory",
    "FwaInventory",
    "GoodsReceipt",
    "IdempotencyRecord",
    "IncentiveEventType",
    "IncentiveLedger",
    "IncentiveRule",
    "InventoryStatus",
    "LedgerStatus",
    "Mpx",
    "MpxCircle",
    "MsisdnPo",
    "MsisdnPoStatus",
    "MsisdnPoStatusHistory",
    "NetworkGeneration",
    "OrgStatus",
    "Region",
    "Shipment",
    "ShipmentMilestone",
    "ShipmentMilestoneType",
    "StockAllocation",
    "StockAllocationItem",
]
