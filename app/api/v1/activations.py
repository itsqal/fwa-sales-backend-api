"""Activation endpoints. Backs Aktivasi Pelanggan and Daftar Aktivasi."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status

from app.core.deps import AppSettings, CurrentAe, DbSession, IdempotencyKey
from app.core.pagination import Pagination, get_pagination
from app.core.periods import Period, resolve_range
from app.models.enums import ActivationStatus
from app.schemas.activation import ActivationCreate, ActivationOut
from app.schemas.common import Paginated
from app.services import activations as activation_service

router = APIRouter(prefix="/activations", tags=["Activations"])


@router.get(
    "",
    summary="List activations submitted by this AE",
    response_model=Paginated[ActivationOut],
)
async def list_activations(
    ae: CurrentAe,
    session: DbSession,
    settings: AppSettings,
    pagination: Annotated[Pagination, Depends(get_pagination)],
    period: Annotated[Period, Query()] = Period.LAST_7_DAYS,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    status_filter: Annotated[list[ActivationStatus] | None, Query(alias="status")] = None,
) -> Paginated[ActivationOut]:
    """The Activated / Not Activated badge is the `status` field; `activationDate` is
    populated only once the Gross Add feed confirms the SIM went live."""
    window = resolve_range(settings, period=period, date_from=date_from, date_to=date_to)
    items, total = await activation_service.list_activations(
        session,
        ae_id=ae.ae_id,
        window=window,
        statuses=status_filter,
        pagination=pagination,
    )
    return Paginated[ActivationOut].model_validate(pagination.envelope(items, total))


@router.post(
    "",
    summary="Submit a customer activation",
    response_model=ActivationOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "This MSISDN has already been activated"},
        422: {"description": "MSISDN_NOT_ALLOCATED or CUSTOMER_NOT_OWNED"},
        401: {"description": "Unauthenticated"},
    },
)
async def create_activation(
    payload: ActivationCreate,
    ae: CurrentAe,
    session: DbSession,
    settings: AppSettings,
    response: Response,
    idempotency_key: IdempotencyKey,
) -> ActivationOut:
    """The client sends only the customer, the scanned MSISDN and the GPS fix.

    IMEI and modem type are resolved server-side from `fwa_inventory` and snapshotted
    onto the record — a client is never trusted to assert which device an MSISDN is
    bundled with. Created with `status: NOT_ACTIVATED`; the GA feed flips it.
    """
    activation, created = await activation_service.create_activation(
        session,
        settings,
        ae_id=ae.ae_id,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    if not created:
        response.headers["Idempotent-Replay"] = "true"
    return activation


@router.get(
    "/{activationId}",
    summary="Retrieve one activation",
    response_model=ActivationOut,
    responses={404: {"description": "Not found, or not this AE's activation"}},
)
async def get_activation(
    activation_id: Annotated[int, Path(alias="activationId", examples=[2987654321])],
    ae: CurrentAe,
    session: DbSession,
) -> ActivationOut:
    return await activation_service.get_activation(
        session, ae_id=ae.ae_id, activation_id=activation_id
    )
