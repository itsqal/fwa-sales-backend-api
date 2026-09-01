"""Read-only master data the dashboard forms are built from.

These are small fixed sets — two brands, four call plans, three device models, six
Device Partners — so they are returned unpaginated. Golden rule 4 (every list is
server-paginated) is about the data lists, which grow without bound; a reference list
that can never exceed a page costs a client more in pagination handling than it saves.
"""

from __future__ import annotations

import uuid

from pydantic import Field

from app.models.enums import CallPlanKind, MpxCircle, NetworkGeneration
from app.schemas.common import CamelModel, nullable_field


class CallPlanOut(CamelModel):
    call_plan_id: uuid.UUID
    code: str = Field(examples=["DATA_50GB", "SALDO_MOBO"])
    name: str = Field(examples=["50 GB", "Saldo Mobo"])
    kind: CallPlanKind = Field(description="BALANCE products carry no quota.")
    quota_gb: int | None = Field(default=None, description="Present only when kind is DATA.")
    sort_order: int


class BrandOut(CamelModel):
    code: str = Field(examples=["IM3", "3ID"])
    display_name: str = Field(examples=["IM3"])
    outlet_name: str = Field(examples=["Gerai IM3"])
    sort_order: int


class DevicePartnerRefOut(CamelModel):
    device_partner_id: uuid.UUID
    code: str = Field(examples=["ADVAN"])
    name: str = Field(examples=["ADVAN"])


class MpxRefOut(CamelModel):
    mpx_id: uuid.UUID
    code: str = Field(examples=["MPX-BKL-01"])
    name: str = Field(examples=["MPX Bengkulu"])
    legal_name: str | None = None
    circle: MpxCircle | None = None
    region_code: str | None = None


class DeviceModelRefOut(CamelModel):
    device_model_id: uuid.UUID
    model_code: str = Field(examples=["ADVAN V1 PRO"])
    brand: str | None = Field(
        default=None,
        description="Hardware manufacturer, not the telco brand.",
        examples=["ADVAN"],
    )
    sku: str | None = None
    network_generation: NetworkGeneration | None = None
    device_partner: DevicePartnerRefOut | None = None
    # Declared oneOf [integer, "null"] in the contract, so the key survives a None: the
    # picker must be able to tell "not priced, cannot be ordered" from "field missing".
    list_price_idr: int | None = nullable_field(
        default=None,
        description="Whole rupiah, or null when the model has no confirmed price.",
    )
    image_url: str | None = None
    is_active: bool
