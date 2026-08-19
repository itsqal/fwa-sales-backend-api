"""Customer endpoints. Backs Input New Customer, Daftar New Customer, Daftar Hot Leads."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status

from app.core.deps import AppSettings, CurrentAe, DbSession, IdempotencyKey
from app.core.pagination import Pagination, get_pagination
from app.core.periods import Period, resolve_range
from app.models.enums import CustomerStatus
from app.schemas.common import Paginated, Wrapped
from app.schemas.customer import (
    CustomerCreate,
    CustomerDetail,
    CustomerLookupItem,
    CustomerOut,
    CustomerUpdate,
)
from app.services import customers as customer_service

router = APIRouter(prefix="/customers", tags=["Customers"])

# The contract spells the path parameter camelCase; Python spells it snake_case.
CustomerIdPath = Annotated[int, Path(alias="customerId", examples=[1234567890])]


@router.get(
    "",
    summary="List prospects registered by this AE",
    response_model=Paginated[CustomerOut],
    responses={401: {"description": "Unauthenticated"}},
)
async def list_customers(
    ae: CurrentAe,
    session: DbSession,
    settings: AppSettings,
    pagination: Annotated[Pagination, Depends(get_pagination)],
    period: Annotated[Period, Query()] = Period.LAST_7_DAYS,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    status_filter: Annotated[list[CustomerStatus] | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(description="Free-text over name and phone.")] = None,
) -> Paginated[CustomerOut]:
    """The Hot Leads screen is this endpoint with `status=HOT_LEADS`.

    An explicit `from`/`to` pair overrides `period`.
    """
    window = resolve_range(settings, period=period, date_from=date_from, date_to=date_to)
    items, total = await customer_service.list_customers(
        session,
        ae_id=ae.ae_id,
        window=window,
        statuses=status_filter,
        query=q,
        pagination=pagination,
    )
    return Paginated[CustomerOut].model_validate(pagination.envelope(items, total))


@router.post(
    "",
    summary="Register a new prospect",
    response_model=CustomerOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Duplicate phone number, or Idempotency-Key reused"},
        422: {"description": "Validation failed"},
        401: {"description": "Unauthenticated"},
    },
)
async def create_customer(
    payload: CustomerCreate,
    ae: CurrentAe,
    session: DbSession,
    settings: AppSettings,
    response: Response,
    idempotency_key: IdempotencyKey,
) -> CustomerOut:
    """`aeId` is taken from the JWT and must not be sent.

    A mocked location is recorded and flagged for supervisor review, never rejected —
    a genuine AE must not be blocked in the field by a GPS quirk.
    """
    customer, created = await customer_service.create_customer(
        session,
        settings,
        ae_id=ae.ae_id,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    if not created:
        # Same status as the original response, so a retrying client sees exactly what
        # it would have seen had the first response not been dropped.
        response.headers["Idempotent-Replay"] = "true"
    return customer


@router.get(
    "/lookup",
    summary='Typeahead source for the "ID Customer" dropdown',
    response_model=Wrapped[list[CustomerLookupItem]],
)
async def lookup_customers(
    ae: CurrentAe,
    session: DbSession,
    q: Annotated[str | None, Query()] = None,
    exclude_activated: Annotated[bool, Query(alias="excludeActivated")] = True,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Wrapped[list[CustomerLookupItem]]:
    items = await customer_service.lookup_customers(
        session,
        ae_id=ae.ae_id,
        query=q,
        exclude_activated=exclude_activated,
        limit=limit,
    )
    return Wrapped[list[CustomerLookupItem]](data=items)


@router.get(
    "/{customerId}",
    summary="Retrieve one prospect",
    response_model=CustomerDetail,
    responses={404: {"description": "Not found, or not this AE's customer"}},
)
async def get_customer(
    customer_id: CustomerIdPath, ae: CurrentAe, session: DbSession
) -> CustomerDetail:
    return await customer_service.get_customer_detail(
        session, ae_id=ae.ae_id, customer_id=customer_id
    )


@router.patch(
    "/{customerId}",
    summary="Update a prospect",
    response_model=CustomerDetail,
    responses={
        404: {"description": "Not found, or not this AE's customer"},
        422: {"description": "Validation failed"},
    },
)
async def update_customer(
    customer_id: CustomerIdPath,
    payload: CustomerUpdate,
    ae: CurrentAe,
    session: DbSession,
) -> CustomerDetail:
    """Chiefly used to move a lead along the funnel — `HOT_LEADS` → `PURCHASE`.

    Every status change is written to `customer_status_history`, which is what makes
    conversion rate measurable later.
    """
    return await customer_service.update_customer(
        session, ae_id=ae.ae_id, customer_id=customer_id, payload=payload
    )
