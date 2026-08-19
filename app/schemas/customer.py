"""Customer request/response bodies. Backs Input New Customer and the Daftar screens."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field, field_validator

from app.core.msisdn import is_valid_phone_number
from app.models.enums import CustomerStatus
from app.schemas.activation import ActivationOut
from app.schemas.common import CamelModel, GeoPoint

_PHONE_DESCRIPTION = (
    "Customer contact number in 08xx or 62xx form. Unlike an FWA MSISDN this is stored "
    "as entered — it is a contact detail, not a device identifier."
)


def _validate_phone(value: str) -> str:
    stripped = value.strip()
    if not is_valid_phone_number(stripped):
        raise ValueError("must match ^(62|0)[0-9]{8,13}$")
    return stripped


class CustomerCreate(CamelModel):
    visit_date: date = Field(description='"Tanggal Pergi" — the day the AE visited.')
    full_name: str = Field(min_length=1, max_length=150, examples=["Susanto"])
    phone_number: str = Field(
        max_length=20, description=_PHONE_DESCRIPTION, examples=["082234567890"]
    )
    address: str = Field(min_length=1, examples=["Jl. Kebahagiaan No. 12"])
    latitude: float = Field(ge=-90, le=90, examples=[-6.2314728])
    longitude: float = Field(ge=-180, le=180, examples=[106.9348415])
    geo_accuracy_m: float | None = Field(default=None, ge=0)
    is_mocked: bool = False
    status: CustomerStatus
    device_time: datetime | None = Field(
        default=None,
        description="Device clock. Recorded for audit, never used for business logic.",
    )

    @field_validator("phone_number")
    @classmethod
    def _check_phone(cls, value: str) -> str:
        return _validate_phone(value)


class CustomerUpdate(CamelModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=150)
    phone_number: str | None = Field(default=None, max_length=20, description=_PHONE_DESCRIPTION)
    address: str | None = Field(default=None, min_length=1)
    status: CustomerStatus | None = None
    status_note: str | None = None

    @field_validator("phone_number")
    @classmethod
    def _check_phone(cls, value: str | None) -> str | None:
        return None if value is None else _validate_phone(value)


class CustomerOut(CamelModel):
    customer_id: int = Field(examples=[1234567890])
    full_name: str = Field(examples=["Susanto"])
    phone_number: str = Field(examples=["082234567890"])
    address: str
    visit_date: date
    status: CustomerStatus
    geo: GeoPoint
    has_activation: bool = Field(
        description="Convenience flag so the list can hide already-activated customers."
    )
    created_at: datetime


class StatusHistoryEntry(CamelModel):
    old_status: CustomerStatus | None = None
    new_status: CustomerStatus
    note: str | None = None
    changed_at: datetime


class CustomerDetail(CustomerOut):
    status_history: list[StatusHistoryEntry] = Field(default_factory=list)
    activations: list[ActivationOut] = Field(default_factory=list)


class CustomerLookupItem(CamelModel):
    customer_id: int = Field(examples=[1234567890])
    full_name: str = Field(examples=["Susanto"])
    label: str = Field(
        description='Rendered as "{customerId} - {fullName}" in the activation dropdown.',
        examples=["1234567890 - Susanto"],
    )
