"""Enumerations shared by the models and the API schemas.

The database stores these as ``VARCHAR`` + ``CHECK`` rather than native PostgreSQL
enum types, so a value can be added in a plain transaction. Every member's name equals
its value, which is what lets SQLAlchemy persist them without a type decorator — the
sole exception is :class:`NetworkGeneration`, whose values start with a digit.
"""

from __future__ import annotations

from enum import StrEnum


class AeStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    INACTIVE = "INACTIVE"


class InventoryStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    ALLOCATED = "ALLOCATED"
    CONSUMED = "CONSUMED"
    ACTIVATED = "ACTIVATED"
    RETURNED = "RETURNED"
    BLOCKED = "BLOCKED"


class NetworkGeneration(StrEnum):
    """Radio generation of a CPE model. Selects the incentive tier on a confirmed GA.

    The one enum here whose member names differ from their values, because a Python
    identifier cannot start with a digit. The column therefore declares
    ``values_callable`` so PostgreSQL stores "4G" / "5G" and not "FOUR_G" / "FIVE_G".
    """

    FOUR_G = "4G"
    FIVE_G = "5G"


class CustomerStatus(StrEnum):
    """i-Sales taxonomy, authoritative for this build.

    The Digital Solution Description calls the same concept
    Potential / Sell In / Non-Potential. That vocabulary is deliberately not used here.
    """

    EDUKASI = "EDUKASI"
    HOT_LEADS = "HOT_LEADS"
    PURCHASE = "PURCHASE"


class ActivationStatus(StrEnum):
    NOT_ACTIVATED = "NOT_ACTIVATED"
    ACTIVATED = "ACTIVATED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ApprovalStatus(StrEnum):
    AUTO_APPROVED = "AUTO_APPROVED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class IncentiveEventType(StrEnum):
    ACTIVATION = "ACTIVATION"
    NEW_CUSTOMER = "NEW_CUSTOMER"
    HOT_LEAD = "HOT_LEAD"


class LedgerStatus(StrEnum):
    ACCRUED = "ACCRUED"
    APPROVED = "APPROVED"
    PAID = "PAID"
    VOID = "VOID"


class IneligibilityReason(StrEnum):
    """Why the submit button on the activation form must stay disabled."""

    NOT_ALLOCATED_TO_YOU = "NOT_ALLOCATED_TO_YOU"
    ALREADY_ACTIVATED = "ALREADY_ACTIVATED"
    BLOCKED = "BLOCKED"
    RETURNED = "RETURNED"
