"""Activation request/response bodies. Backs Aktivasi Pelanggan and Daftar Aktivasi."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.core.msisdn import normalise_msisdn
from app.models.enums import ActivationStatus
from app.schemas.common import CamelModel, GeoPoint, nullable_field


class ActivationCreate(CamelModel):
    """What the client is allowed to assert: a customer, a scanned number, a GPS fix.

    Notably absent are IMEI and modem type. The server resolves those from
    ``fwa_inventory`` — trusting the client with them would let an AE fabricate stock.
    """

    customer_id: int = Field(examples=[1234567890])
    msisdn: str = Field(
        max_length=20,
        description="Scanned from the box barcode. Normalised to 62 form on arrival.",
        examples=["6285882724305"],
    )
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    geo_accuracy_m: float | None = Field(default=None, ge=0)
    is_mocked: bool = False
    device_time: datetime | None = Field(
        default=None,
        description="Device clock. Recorded for audit, never used for business logic.",
    )

    @field_validator("msisdn")
    @classmethod
    def _normalise(cls, value: str) -> str:
        normalised = normalise_msisdn(value)
        if normalised is None:
            raise ValueError("must be an Indonesian MSISDN matching ^62[0-9]{8,13}$")
        return normalised


class ActivationCustomerOut(CamelModel):
    customer_id: int = Field(examples=[1234567890])
    full_name: str = Field(examples=["Susanto"])


class ActivationOut(CamelModel):
    activation_id: int = Field(examples=[2987654321])
    customer: ActivationCustomerOut
    msisdn: str = Field(examples=["6289999999990"])
    imei: str = Field(examples=["355806671396654"])
    device_model_code: str | None = Field(default=None, examples=["HKM 127+"])
    geo: GeoPoint
    submitted_at: datetime
    status: ActivationStatus
    # Declared oneOf [date-time, null] in the contract: the key is always present, and
    # the client reads null as "submitted, not yet live" — the red badge.
    activation_date: datetime | None = nullable_field(
        default=None, description="GA Date. Null until the Gross Add feed confirms."
    )
