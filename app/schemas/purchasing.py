"""Request and response bodies for the two purchase-order flows.

No request body anywhere here carries a `status`, a `poCode`, or an organisation id.
Status moves only through named transition endpoints (golden rule 8), PO codes are
generated server-side, and the org binding comes from the token (golden rule 7).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.models.enums import DevicePoStatus, MsisdnPoStatus
from app.schemas.common import CamelModel, nullable_field
from app.schemas.reference import BrandOut, CallPlanOut, DevicePartnerRefOut, MpxRefOut

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


class StatusHistoryOut(CamelModel):
    """One row of the *Riwayat* panel: tanggal, status, oleh, keterangan."""

    changed_at: datetime
    old_status: str | None = None
    new_status: str
    changed_by: str | None = Field(
        default=None,
        description="Full name of the admin who made the change — the *oleh* column.",
    )
    note: str | None = Field(
        default=None,
        description='The *keterangan* column. The shipment step writes "J&T Express | JD046…".',
    )


class RejectRequest(CamelModel):
    reason: str = Field(
        min_length=1,
        max_length=500,
        description="Required. Surfaced to the counterparty so they can resubmit correctly.",
    )


class NoteRequest(CamelModel):
    note: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# MSISDN PO
# ---------------------------------------------------------------------------


class CreateMsisdnPoRequest(CamelModel):
    call_plan_id: uuid.UUID
    brand_code: str = Field(min_length=1, max_length=10, examples=["IM3"])
    qty_requested: int = Field(gt=0, le=100_000, description="Numbers requested.")
    note: str | None = Field(default=None, max_length=500)


class MsisdnPoOut(CamelModel):
    msisdn_po_id: uuid.UUID
    po_code: str = Field(examples=["ADVAN-20250523-207"])
    device_partner: DevicePartnerRefOut
    call_plan: CallPlanOut
    brand: BrandOut
    qty_requested: int
    qty_supplied: int = Field(
        description="Numbers actually issued against this PO. Zero until IOH supplies."
    )
    status: MsisdnPoStatus
    note: str | None = None
    # Declared oneOf [string, "null"]: the DP list renders the refusal reason inline, so
    # the key must be present and null rather than missing when there is no refusal.
    rejected_reason: str | None = nullable_field(default=None)
    submitted_at: datetime
    processed_at: datetime | None = None
    completed_at: datetime | None = None


class MsisdnPoDetailOut(MsisdnPoOut):
    status_history: list[StatusHistoryOut]


class SupplyRequest(CamelModel):
    """The bulk supply payload.

    MSISDNs arrive as a JSON array, not as a spreadsheet: the dashboard parses the XLS
    and posts what it parsed, which is also what it showed the operator in the
    validate step. Parsing server-side would mean an XLS library, and this project does
    not add a dependency without asking.
    """

    msisdns: list[str] = Field(
        min_length=1,
        max_length=100_000,
        description="Accepted in 08… or 62… form; normalised to 62 before validation.",
        examples=[["6285882724305", "085882724306"]],
    )


class SupplyValidationOut(CamelModel):
    """Dry run. Nothing is written, and this is what the review step renders."""

    ok: bool
    expected: int
    received: int
    accepted: list[str] = Field(description="Normalised to 62 form.")
    errors: list[dict[str, str]] = Field(
        default_factory=list,
        description="One entry per rejected row: the value and why it was rejected.",
    )


class SupplyResultOut(CamelModel):
    msisdn_po_id: uuid.UUID
    status: MsisdnPoStatus
    supplied: int


class MsisdnOut(CamelModel):
    """One issued number. Backs the download beside a supplied PO."""

    msisdn: str
    imei: str | None = nullable_field(default=None, description="Null until DP pairs one.")
    status: str
    paired_at: datetime | None = None


# ---------------------------------------------------------------------------
# Hard-bundle pairing
# ---------------------------------------------------------------------------


class PairingRow(CamelModel):
    msisdn: str = Field(examples=["6285882724305"])
    imei: str = Field(min_length=14, max_length=16, examples=["355806671396654"])


class PairingRequest(CamelModel):
    pairs: list[PairingRow] = Field(min_length=1, max_length=100_000)


class PairingValidationOut(CamelModel):
    ok: bool
    expected: int
    received: int
    accepted: list[PairingRow]
    errors: list[dict[str, str]] = Field(default_factory=list)


class PairingResultOut(CamelModel):
    msisdn_po_id: uuid.UUID
    paired: int


# ---------------------------------------------------------------------------
# Device PO
# ---------------------------------------------------------------------------


class CreateDevicePoRequest(CamelModel):
    device_partner_id: uuid.UUID
    device_model_id: uuid.UUID
    brand_code: str = Field(min_length=1, max_length=10, examples=["3ID"])
    qty: int = Field(gt=0, le=100_000)
    address_id: uuid.UUID = Field(description="Must belong to the calling MPX.")
    pic_name: str | None = Field(default=None, max_length=150)
    pic_phone: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=500)


class DevicePoOut(CamelModel):
    device_po_id: uuid.UUID
    po_code: str = Field(examples=["PO-JAVA-3ID-RABIT CPE-R-28-20250809-407"])
    mpx: MpxRefOut
    device_partner: DevicePartnerRefOut
    device_model_code: str
    brand: BrandOut
    qty: int
    qty_attached: int = Field(
        description="Bundles the DP has attached so far. Must equal qty before shipping."
    )
    unit_price_idr: int = Field(description="Whole rupiah, snapshotted at creation.")
    total_idr: int
    status: DevicePoStatus
    pic_name: str | None = None
    pic_phone: str | None = None
    note: str | None = None
    rejected_reason: str | None = nullable_field(default=None)
    submitted_at: datetime
    accepted_at: datetime | None = None
    completed_at: datetime | None = None


class AddressOut(CamelModel):
    address_id: uuid.UUID
    label: str
    recipient_name: str
    recipient_phone: str
    line1: str
    kelurahan: str | None = None
    kecamatan: str | None = None
    city: str
    province: str
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    gmaps_url: str | None = None
    is_default: bool


class DevicePoDetailOut(DevicePoOut):
    address: AddressOut
    status_history: list[StatusHistoryOut]


class AttachBundlesRequest(CamelModel):
    """Bundles the DP is committing to this order, by MSISDN."""

    msisdns: list[str] = Field(min_length=1, max_length=100_000)


class AttachBundlesValidationOut(CamelModel):
    ok: bool
    expected: int
    received: int
    accepted: list[str]
    errors: list[dict[str, str]] = Field(default_factory=list)


class AttachBundlesResultOut(CamelModel):
    device_po_id: uuid.UUID
    status: DevicePoStatus
    attached: int


class BundleOut(CamelModel):
    """A paired MSISDN+IMEI unit. Backs the *Detail IMEI & MSISDN* modal."""

    msisdn: str
    imei: str | None = nullable_field(default=None)
    device_model_code: str | None = None
    brand_code: str | None = None
    status: str
    paired_at: datetime | None = None
    received_at: datetime | None = None
