"""Attendance (CICO) endpoints."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from app.core.deps import AppSettings, CurrentAe, DbSession, IdempotencyKey
from app.core.files import DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS, store_file
from app.core.pagination import Pagination, get_pagination
from app.core.periods import validate_optional_range
from app.schemas.attendance import AttendanceOut, CheckOutRequest
from app.schemas.common import Paginated, Wrapped
from app.services import attendance as attendance_service

router = APIRouter(tags=["Attendance"])


@router.post(
    "/attendance/check-in",
    summary="Daily check-in with selfie and location",
    response_model=AttendanceOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Already checked in today"},
        422: {"description": "Validation failed"},
    },
)
async def check_in(
    ae: CurrentAe,
    session: DbSession,
    settings: AppSettings,
    response: Response,
    idempotency_key: IdempotencyKey,
    photo: Annotated[UploadFile, File(description="Selfie. Mandatory on check-in.")],
    latitude: Annotated[float, Form(ge=-90, le=90)],
    longitude: Annotated[float, Form(ge=-180, le=180)],
    geo_accuracy_m: Annotated[float | None, Form(alias="geoAccuracyM", ge=0)] = None,
    is_mocked: Annotated[bool, Form(alias="isMocked")] = False,
    note: Annotated[str | None, Form(description="Catatan (Opsional)")] = None,
    document: Annotated[UploadFile | None, File(description="Dokumen (Opsional)")] = None,
    device_time: Annotated[datetime | None, Form(alias="deviceTime")] = None,
) -> AttendanceOut:
    """Multipart because of the mandatory selfie. Compress client-side to roughly
    1024px / 70% quality before upload — raw phone photos fail on a field connection.

    The returned `checkInPhotoUrl` is a short-lived signed URL, not a public path.
    """
    # geoAccuracyM is accepted because the contract declares it, but there is no column
    # for it on `attendance` — unlike customer and activation, which both carry one.
    # Adding one is a schema change, so it is reported rather than done quietly.
    photo_key = store_file(
        settings,
        data=await photo.read(),
        filename=photo.filename,
        category="attendance/photo",
        allowed_extensions=IMAGE_EXTENSIONS,
        field_name="photo",
    )
    document_key = (
        store_file(
            settings,
            data=await document.read(),
            filename=document.filename,
            category="attendance/document",
            allowed_extensions=DOCUMENT_EXTENSIONS,
            field_name="document",
        )
        if document is not None and document.filename
        else None
    )

    attendance, created = await attendance_service.check_in(
        session,
        settings,
        ae_id=ae.ae_id,
        latitude=latitude,
        longitude=longitude,
        is_mocked=is_mocked,
        note=note,
        photo_key=photo_key,
        document_key=document_key,
        device_time=device_time,
        idempotency_key=idempotency_key,
    )
    if not created:
        response.headers["Idempotent-Replay"] = "true"
    return attendance


@router.post(
    "/attendance/check-out",
    summary="Daily check-out",
    response_model=AttendanceOut,
    responses={409: {"description": "No open check-in for today"}},
)
async def check_out(
    payload: CheckOutRequest,
    ae: CurrentAe,
    session: DbSession,
    settings: AppSettings,
) -> AttendanceOut:
    return await attendance_service.check_out(session, settings, ae_id=ae.ae_id, payload=payload)


@router.get(
    "/attendance/today",
    summary="Today's attendance record",
    response_model=Wrapped[AttendanceOut | None],
)
async def get_today_attendance(
    ae: CurrentAe, session: DbSession, settings: AppSettings
) -> Wrapped[AttendanceOut | None]:
    """`null` data means not yet checked in."""
    record = await attendance_service.get_today(session, settings, ae_id=ae.ae_id)
    return Wrapped[AttendanceOut | None](data=record)


@router.get(
    "/attendance",
    summary="Attendance history",
    response_model=Paginated[AttendanceOut],
)
async def list_attendance(
    ae: CurrentAe,
    session: DbSession,
    settings: AppSettings,
    pagination: Annotated[Pagination, Depends(get_pagination)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> Paginated[AttendanceOut]:
    validate_optional_range(date_from, date_to)
    items, total = await attendance_service.list_attendance(
        session,
        settings,
        ae_id=ae.ae_id,
        date_from=date_from,
        date_to=date_to,
        pagination=pagination,
    )
    return Paginated[AttendanceOut].model_validate(pagination.envelope(items, total))
