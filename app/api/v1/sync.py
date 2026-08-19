"""Offline bootstrap endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import AppSettings, CurrentAe, DbSession
from app.schemas.sync import BootstrapResponse
from app.services import sync as sync_service

router = APIRouter(prefix="/sync", tags=["Sync"])


@router.get(
    "/bootstrap",
    summary="One-shot payload to prime the offline cache",
    response_model=BootstrapResponse,
)
async def sync_bootstrap(
    ae: CurrentAe,
    session: DbSession,
    settings: AppSettings,
    since: Annotated[
        datetime | None, Query(description="Return only records changed after this instant.")
    ] = None,
) -> BootstrapResponse:
    """Called on login and on pull-to-refresh: profile, allocated stock, recent
    customers, and the server clock for drift correction."""
    return await sync_service.bootstrap(session, settings, ae=ae, since=since)
