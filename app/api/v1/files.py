"""Signed-URL file delivery.

**This endpoint is an addition to the reviewed contract**, recorded in
``docs/openapi.yaml`` in the same change. It exists because ``Attendance.checkInPhotoUrl``
has to resolve to something, and §9 requires that something to be a short-lived signed
URL rather than a public bucket path.

There is deliberately no bearer auth here: the token *is* the authorisation, it expires
in minutes, and it is only ever handed to the AE who owns the record. That is what lets
an ``<img src>`` in the app load the photo without attaching an Authorization header.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.core.deps import AppSettings
from app.core.errors import NotFoundError
from app.core.files import SignedUrlError, resolve_token

router = APIRouter(prefix="/files", tags=["Files"])


@router.get(
    "/{token}",
    summary="Fetch a private file with a short-lived signed token",
    response_class=FileResponse,
    responses={404: {"description": "Token invalid, expired, or file unknown"}},
)
async def get_file(token: str, settings: AppSettings) -> FileResponse:
    try:
        path, content_type = resolve_token(settings, token)
    except SignedUrlError as exc:
        # A tampered token and a missing file give the same answer. Distinguishing them
        # would turn this into an oracle for which storage keys exist.
        raise NotFoundError("This link is no longer valid.", code="FILE_NOT_FOUND") from exc

    return FileResponse(
        path,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=60"},
    )
