"""Server-to-server endpoints. Not reachable by the mobile app."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.core.deps import AppSettings, DbSession, require_service_credential
from app.schemas.internal import GaEventBatch, GaIngestResult
from app.services import ga as ga_service

router = APIRouter(prefix="/internal", tags=["Internal"])


@router.post(
    "/ga-events",
    summary="Ingest Gross Add events from GCP",
    response_model=GaIngestResult,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_service_credential)],
    responses={403: {"description": "Missing or invalid service credential"}},
)
async def ingest_ga_events(
    payload: GaEventBatch,
    session: DbSession,
    settings: AppSettings,
) -> GaIngestResult:
    """Flips matching activations to `ACTIVATED` and stamps `activationDate` — the
    brief's "GA Date — Automatic from GCP". Also the trigger for incentive accrual.

    Requires a service credential and should additionally be blocked from the public
    app path at the reverse proxy.
    """
    return await ga_service.ingest(session, settings, events=payload.events)
