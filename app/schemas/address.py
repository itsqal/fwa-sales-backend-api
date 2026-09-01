"""Delivery address book request bodies.

`mpxId` is deliberately absent: the address belongs to whichever MPX is calling, taken
from the token. Accepting it would be exactly the parameter golden rule 7 forbids.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from app.schemas.common import CamelModel


class CreateAddressRequest(CamelModel):
    label: str = Field(min_length=1, max_length=60, examples=["Gudang Utama"])
    recipient_name: str = Field(min_length=1, max_length=150)
    recipient_phone: str = Field(
        min_length=1,
        max_length=20,
        pattern=r"^(62|0)[0-9]{8,13}$",
        description="A contact number, stored as typed in 08… or 62… form.",
        examples=["081234567890"],
    )
    line1: str = Field(min_length=1)
    kelurahan: str | None = Field(default=None, max_length=120)
    kecamatan: str | None = Field(default=None, max_length=120)
    city: str = Field(min_length=1, max_length=120)
    province: str = Field(min_length=1, max_length=120)
    postal_code: str | None = Field(default=None, max_length=10)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    gmaps_url: str | None = None
    is_default: bool = False

    @model_validator(mode="after")
    def _coordinates_come_as_a_pair(self) -> CreateAddressRequest:
        """Half a coordinate cannot be plotted, so it is rejected here as well as by
        ``ck_address_geo_pair`` — the constraint is the guarantee, this is the readable
        error."""
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        return self
