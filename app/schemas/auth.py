"""Auth request/response bodies."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field

from app.models.enums import AeStatus
from app.schemas.common import CamelModel


class LoginRequest(CamelModel):
    ae_code: str = Field(min_length=1, max_length=50, examples=["AE-BENGKULU1"])
    password: str = Field(min_length=1)
    device_label: str | None = Field(
        default=None,
        max_length=120,
        description="Free-text device identifier, stored against the refresh token.",
        examples=["Samsung A15 / android-14"],
    )


class RefreshRequest(CamelModel):
    refresh_token: str = Field(min_length=1)


class RegionOut(CamelModel):
    region_code: str = Field(examples=["BENGKULU"])
    region_name: str = Field(examples=["Bengkulu"])


class WorkShiftOut(CamelModel):
    start: str = Field(examples=["08:00"])
    end: str = Field(examples=["18:00"])


class AccountExecutiveOut(CamelModel):
    ae_id: uuid.UUID
    ae_code: str = Field(examples=["AE-BENGKULU1"])
    full_name: str = Field(examples=["Hendra Jaya"])
    role: Literal["AE"] = "AE"
    region: RegionOut | None = None
    mpx_code: str | None = None
    work_shift: WorkShiftOut
    status: AeStatus


class AuthTokens(CamelModel):
    access_token: str
    refresh_token: str
    expires_in: int = Field(description="Access-token lifetime in seconds.", examples=[900])
    must_change_password: bool = Field(
        description="True on first login with an HQ-issued password."
    )
    profile: AccountExecutiveOut
