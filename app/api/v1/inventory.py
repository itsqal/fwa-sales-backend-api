"""Inventory endpoints. Backs the barcode scanner on the activation form."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.core.deps import CurrentAe, DbSession
from app.core.errors import NotFoundError
from app.core.msisdn import normalise_msisdn
from app.core.pagination import Pagination, get_pagination
from app.models.enums import InventoryStatus
from app.schemas.common import Paginated
from app.schemas.inventory import InventoryItemOut
from app.services import inventory as inventory_service

router = APIRouter(prefix="/inventory", tags=["Inventory"])


@router.get(
    "/msisdn/{msisdn}",
    summary="Resolve a scanned MSISDN to its bundled device",
    response_model=InventoryItemOut,
    responses={
        404: {"description": "MSISDN is not a registered FWA number"},
        401: {"description": "Unauthenticated"},
    },
)
async def lookup_msisdn(
    ae: CurrentAe,
    session: DbSession,
    msisdn: Annotated[str, Path(description="Normalised to 62 form before the call.")],
) -> InventoryItemOut:
    """Called the moment the barcode scanner reads a number. This is what populates the
    read-only **IMEI** and **Tipe Modem** fields on the activation form.

    Gate the submit button on `eligible`; `reason` says why it is false.
    """
    normalised = normalise_msisdn(msisdn)
    if normalised is None:
        # An unparseable scan cannot match any row, so this is the same answer as a
        # lookup miss rather than a separate validation failure.
        raise NotFoundError(
            "This number is not a registered HiFi AIR unit.", code="MSISDN_NOT_FOUND"
        )
    return await inventory_service.lookup_msisdn(session, ae_id=ae.ae_id, msisdn=normalised)


@router.get(
    "/me",
    summary="Stock allocated to this AE",
    response_model=Paginated[InventoryItemOut],
)
async def list_my_inventory(
    ae: CurrentAe,
    session: DbSession,
    pagination: Annotated[Pagination, Depends(get_pagination)],
    status: Annotated[InventoryStatus | None, Query()] = None,
) -> Paginated[InventoryItemOut]:
    items, total = await inventory_service.list_my_inventory(
        session, ae_id=ae.ae_id, status=status, pagination=pagination
    )
    return Paginated[InventoryItemOut].model_validate(pagination.envelope(items, total))
