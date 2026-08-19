"""Attendance (CICO).

On the geofence: check-in does **not** evaluate one, and that is deliberate rather than
unfinished. Two inputs are missing, not one — the tolerance in metres is an open
business decision (CLAUDE.md §11.2), and the schema carries no reference point to
measure from: neither ``account_executive`` nor ``region`` holds a coordinate. Guessing
either would put a number nobody agreed to in front of a supervisor as fact. Until both
exist, ``within_geofence`` is written NULL and check-ins are ``AUTO_APPROVED``; the
``PENDING_APPROVAL`` path is modelled and ready.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_device_time
from app.core.config import Settings
from app.core.errors import ConflictError
from app.core.files import build_url
from app.core.pagination import Pagination
from app.core.periods import today
from app.models.attendance import Attendance
from app.models.enums import ApprovalStatus
from app.schemas.attendance import AttendanceOut, CheckOutRequest
from app.schemas.common import GeoPoint


def to_attendance_out(settings: Settings, attendance: Attendance) -> AttendanceOut:
    return AttendanceOut(
        attendance_id=str(attendance.attendance_id),
        attendance_date=attendance.attendance_date,
        check_in_at=attendance.check_in_at,
        check_in_photo_url=(
            build_url(settings, attendance.check_in_photo_url)
            if attendance.check_in_photo_url
            else None
        ),
        check_in_geo=(
            GeoPoint(
                latitude=attendance.check_in_lat,
                longitude=attendance.check_in_lng,
                is_mocked=attendance.check_in_is_mocked,
            )
            if attendance.check_in_lat is not None and attendance.check_in_lng is not None
            else None
        ),
        check_in_address=attendance.check_in_address,
        within_geofence=attendance.within_geofence,
        check_out_at=attendance.check_out_at,
        note=attendance.note,
        document_url=(
            build_url(settings, attendance.document_url) if attendance.document_url else None
        ),
        approval_status=attendance.approval_status,
    )


async def check_in(
    session: AsyncSession,
    settings: Settings,
    *,
    ae_id: uuid.UUID,
    latitude: float,
    longitude: float,
    is_mocked: bool,
    note: str | None,
    photo_key: str,
    document_key: str | None,
    device_time: datetime | None,
    idempotency_key: uuid.UUID | None,
) -> tuple[AttendanceOut, bool]:
    """Record the day's check-in. Returns ``(attendance, created)``."""
    record_device_time(ae_id=ae_id, action="attendance.check_in", device_time=device_time)
    attendance_date = today(settings)

    if idempotency_key is not None:
        existing = await session.scalar(
            select(Attendance).where(Attendance.idempotency_key == idempotency_key)
        )
        if existing is not None:
            if existing.ae_id != ae_id:
                raise ConflictError(
                    "This Idempotency-Key was already used with a different request.",
                    code="IDEMPOTENCY_KEY_REUSED",
                )
            return to_attendance_out(settings, existing), False

    attendance = Attendance(
        ae_id=ae_id,
        attendance_date=attendance_date,
        # Server time. A phone with a wrong clock cannot backdate its own attendance.
        check_in_at=datetime.now(UTC),
        check_in_photo_url=photo_key,
        check_in_lat=latitude,
        check_in_lng=longitude,
        check_in_is_mocked=is_mocked,
        within_geofence=None,
        note=note,
        document_url=document_key,
        approval_status=ApprovalStatus.AUTO_APPROVED,
        idempotency_key=idempotency_key,
    )
    session.add(attendance)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        constraint = str(getattr(exc.orig, "constraint_name", "") or exc.orig or "")
        if "uq_att_idem" in constraint:
            raise ConflictError(
                "This Idempotency-Key was already used with a different request.",
                code="IDEMPOTENCY_KEY_REUSED",
            ) from exc
        raise ConflictError(
            "You have already checked in today.", code="ALREADY_CHECKED_IN"
        ) from exc

    return to_attendance_out(settings, attendance), True


async def check_out(
    session: AsyncSession,
    settings: Settings,
    *,
    ae_id: uuid.UUID,
    payload: CheckOutRequest,
) -> AttendanceOut:
    record_device_time(ae_id=ae_id, action="attendance.check_out", device_time=payload.device_time)

    attendance = await session.scalar(
        select(Attendance).where(
            Attendance.ae_id == ae_id, Attendance.attendance_date == today(settings)
        )
    )
    if attendance is None or attendance.check_in_at is None:
        raise ConflictError("You have not checked in today.", code="NO_OPEN_CHECK_IN")
    if attendance.check_out_at is not None:
        raise ConflictError("You have already checked out today.", code="ALREADY_CHECKED_OUT")

    attendance.check_out_at = datetime.now(UTC)
    attendance.check_out_lat = payload.latitude
    attendance.check_out_lng = payload.longitude
    await session.flush()
    return to_attendance_out(settings, attendance)


async def get_today(
    session: AsyncSession, settings: Settings, *, ae_id: uuid.UUID
) -> AttendanceOut | None:
    attendance = await session.scalar(
        select(Attendance).where(
            Attendance.ae_id == ae_id, Attendance.attendance_date == today(settings)
        )
    )
    return to_attendance_out(settings, attendance) if attendance is not None else None


async def list_attendance(
    session: AsyncSession,
    settings: Settings,
    *,
    ae_id: uuid.UUID,
    date_from: date | None,
    date_to: date | None,
    pagination: Pagination,
) -> tuple[list[AttendanceOut], int]:
    filters = [Attendance.ae_id == ae_id]
    if date_from is not None:
        filters.append(Attendance.attendance_date >= date_from)
    if date_to is not None:
        filters.append(Attendance.attendance_date <= date_to)

    total = await session.scalar(select(func.count()).select_from(Attendance).where(*filters))

    rows = (
        await session.scalars(
            select(Attendance)
            .where(*filters)
            .order_by(Attendance.attendance_date.desc())
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
    ).all()

    return [to_attendance_out(settings, row) for row in rows], int(total or 0)
