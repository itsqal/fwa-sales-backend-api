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


class BrandScope(StrEnum):
    """Which telco brands an Account Executive may sell.

    ``HYBRID`` means either. It is a capability of a person and deliberately NOT a row
    in the ``brand`` table: a physical SIM is one brand or the other, so a hybrid unit
    would be meaningless, and putting it there would let it leak into every
    ``brand_code`` column in the supply chain.
    """

    IM3 = "IM3"
    THREE_ID = "3ID"
    HYBRID = "HYBRID"


class AdminRole(StrEnum):
    """Web dashboard principal type.

    Each role implies exactly one organisation binding on ``admin_user``, enforced by
    ``ck_admin_org_binding``. ``IOH_ADMIN`` is the only global-read role, and it is
    read-only over Device Partner and MPX data.
    """

    DP_ADMIN = "DP_ADMIN"
    IOH_ADMIN = "IOH_ADMIN"
    MPX_ADMIN = "MPX_ADMIN"


class AdminStatus(StrEnum):
    """Same three values as :class:`AeStatus`, kept separate so the two can diverge.

    An admin is deactivated by a different process from a field salesman.
    """

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    INACTIVE = "INACTIVE"


class OrgStatus(StrEnum):
    """Lifecycle of a counterparty organisation — a Device Partner or an MPX."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class MsisdnPoStatus(StrEnum):
    """DP requests numbers from IOH.

    ::

        DIAJUKAN -> DIPROSES -> DITERIMA
            |           |
        DIBATALKAN   DITOLAK

    Canonical values, per Supply Chain spec section 6. The mockups use different
    vocabulary per role; the UI translates for display and the API never does.
    """

    DIAJUKAN = "DIAJUKAN"
    DIPROSES = "DIPROSES"
    DITERIMA = "DITERIMA"
    DITOLAK = "DITOLAK"
    DIBATALKAN = "DIBATALKAN"


class DevicePoStatus(StrEnum):
    """MPX orders devices from a DP.

    ::

        DIAJUKAN -> DIPROSES -> DIKIRIM -> PERIKSA -> DITERIMA
            |           |
        DIBATALKAN   DITOLAK

    ``DITERIMA_SEBAGIAN`` is deliberately absent. The spec modelled a partial-receipt
    state, but the business rule confirmed on 2026-09-01 is that MPX can only confirm
    receipt when every unit on the order is present: one unit short and the receipt
    cannot complete. A state that can never be reached is worse than no state.

    The DP screen labels DIKIRIM as "Delivery" and the MPX screen as "Dikirim". One
    state, two words; DIKIRIM is canonical and the DP label is a client-side bug.
    """

    DIAJUKAN = "DIAJUKAN"
    DIPROSES = "DIPROSES"
    DIKIRIM = "DIKIRIM"
    PERIKSA = "PERIKSA"
    DITERIMA = "DITERIMA"
    DITOLAK = "DITOLAK"
    DIBATALKAN = "DIBATALKAN"


class ShipmentMilestoneType(StrEnum):
    """The four steps of the Delivery Progress tracker, in order.

    Entered by hand by the Device Partner for v1 — there is no courier integration, so
    nothing outside the dashboard writes these.
    """

    SHIPPED = "SHIPPED"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"


class AllocationMode(StrEnum):
    """How the units in an allocation were chosen.

    ``AUTO`` takes the oldest received stock first, which the allocation modal states
    outright: *alokasi akan mengutamakan modem yang masuk stok lebih awal*. ``MANUAL``
    means the MPX admin named each unit.
    """

    AUTO = "AUTO"
    MANUAL = "MANUAL"


class CallPlanKind(StrEnum):
    """What a call plan actually grants.

    ``BALANCE`` exists because *Saldo Mobo* is a balance top-up and not a data bundle.
    Without the discriminator it becomes a special case in every consumer that reads
    ``quota_gb``.
    """

    DATA = "DATA"
    BALANCE = "BALANCE"


class MpxCircle(StrEnum):
    """Indosat sales circle an MPX belongs to."""

    JAVA = "JAVA"
    KALISUMAPA = "KALISUMAPA"
    SUMATERA = "SUMATERA"


class InventoryStatus(StrEnum):
    """The whole life of one unit, from an issued number to an activated customer.

    The first six values predate the supply chain and are what the mobile app knows.
    The five added in migration 0005 are upstream of it: a unit in any of them is not
    yet a salesman's to sell, and is filtered out of every AE-facing read by
    :data:`app.services.inventory.AE_VISIBLE_STATUSES`.
    """

    # --- Deprecated. Retained because live rows still carry them, and removing a value
    # would mean rewriting the CHECK against those rows.
    AVAILABLE = "AVAILABLE"  # predates allocation being explicit
    CONSUMED = "CONSUMED"  # now written only by the activation path

    # --- Supply chain, invisible to the AE app. Set by the dashboard.
    MSISDN_ISSUED = "MSISDN_ISSUED"  # IOH supplied the number; no IMEI yet
    PAIRED = "PAIRED"  # DP bundled an IMEI to it
    ASSIGNED = "ASSIGNED"  # DP attached the bundle to a device PO
    SHIPPED = "SHIPPED"  # DP handed it to a courier
    RECEIVED = "RECEIVED"  # MPX confirmed receipt; allocatable stock

    # --- Visible to the AE app. ALLOCATED is the seam: the first status a salesman
    # can see, written only by POST /admin/allocations.
    ALLOCATED = "ALLOCATED"
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
