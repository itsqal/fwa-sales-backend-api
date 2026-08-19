"""Attendance (CICO) request/response bodies."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from app.models.enums import ApprovalStatus
from app.schemas.common import CamelModel, GeoPoint, nullable_field


class CheckOutRequest(CamelModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    is_mocked: bool = False
    device_time: datetime | None = Field(
        default=None,
        description="Device clock. Recorded for audit, never used for business logic.",
    )


class AttendanceOut(CamelModel):
    attendance_id: str
    attendance_date: date
    check_in_at: datetime | None = None
    check_in_photo_url: str | None = Field(
        default=None,
        description=(
            "Short-lived signed URL. It expires — fetch the record again rather than "
            "caching the link."
        ),
    )
    check_in_geo: GeoPoint | None = None
    check_in_address: str | None = None
    within_geofence: bool | None = None
    # Declared oneOf [date-time, null]: null distinguishes "still out in the field"
    # from a day with no record at all.
    check_out_at: datetime | None = nullable_field(default=None)
    note: str | None = None
    document_url: str | None = None
    approval_status: ApprovalStatus
