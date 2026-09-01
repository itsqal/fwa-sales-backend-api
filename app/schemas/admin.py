"""Admin auth request/response bodies for the supply chain web dashboard.

Mirrors ``app/schemas/auth.py``. The one structural difference is
:class:`AdminProfile.organisation`: an AE belongs to a region, an admin is *bound* to
exactly one counterparty, and that binding is the entire authorisation context for
every downstream endpoint.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field

from app.models.enums import AdminRole, AdminStatus, MpxCircle
from app.schemas.common import CamelModel, nullable_field


class AdminLoginRequest(CamelModel):
    username: str = Field(min_length=1, max_length=50, examples=["dp.advan"])
    password: str = Field(min_length=1)
    device_label: str | None = Field(
        default=None,
        max_length=120,
        description="Free-text browser identifier, stored against the refresh token.",
        examples=["Chrome 141 / Windows"],
    )


class AdminRefreshRequest(CamelModel):
    refresh_token: str = Field(min_length=1)


class AdminOrganisationOut(CamelModel):
    """The single Device Partner or MPX this principal may act for."""

    type: Literal["DEVICE_PARTNER", "MPX"]
    id: uuid.UUID
    code: str = Field(examples=["ADVAN", "MPX-BKL-01"])
    name: str = Field(examples=["ADVAN", "MPX Bengkulu"])
    legal_name: str | None = Field(
        default=None,
        description="MPX only. Shown under the user name in the topbar.",
        examples=["PT Internet Rakyat Makmur"],
    )
    circle: MpxCircle | None = Field(default=None, description="MPX only.")


class AdminProfileOut(CamelModel):
    admin_user_id: uuid.UUID
    username: str = Field(examples=["dp.advan"])
    email: str | None = None
    full_name: str = Field(
        description=(
            "Rendered verbatim into every attestation checkbox and recorded as the "
            "`oleh` column of each status-history row."
        ),
        examples=["Atha Marcella"],
    )
    role: AdminRole
    # Declared oneOf [AdminOrganisation, "null"] in the contract, so the key survives a
    # None. The dashboard builds its sidebar from this response, and "IOH_ADMIN is bound
    # to no counterparty" must not arrive looking like "the field was not returned".
    organisation: AdminOrganisationOut | None = nullable_field(
        default=None,
        description="Null only for IOH_ADMIN, which is global-read by design.",
    )
    status: AdminStatus


class AdminAuthTokens(CamelModel):
    access_token: str
    refresh_token: str
    expires_in: int = Field(description="Access-token lifetime in seconds.", examples=[900])
    must_change_password: bool = Field(
        description="True on first login with an HQ-issued password."
    )
    profile: AdminProfileOut
