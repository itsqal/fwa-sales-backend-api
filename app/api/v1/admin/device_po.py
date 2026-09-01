"""Device PO endpoints — step 7.

The MPX side raises and cancels; the DP side accepts, rejects, and attaches bundles.
Both see the same order through the same status machine, which is why one router serves
two roles rather than two routers duplicating it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.core.deps import CurrentAdmin, DbSession, IdempotencyKey
from app.core.pagination import Pagination, get_pagination
from app.models.enums import DevicePoStatus
from app.schemas.common import Paginated, Wrapped
from app.schemas.purchasing import (
    AttachBundlesRequest,
    AttachBundlesResultOut,
    AttachBundlesValidationOut,
    BundleOut,
    CreateDevicePoRequest,
    DevicePoDetailOut,
    DevicePoOut,
    NoteRequest,
    RejectRequest,
)
from app.services import device_po as service
from app.services.supply_common import hash_request, remember, replay

router = APIRouter(prefix="/device-pos", tags=["Device PO"])


@router.get("", summary="List device orders", response_model=Paginated[DevicePoOut])
async def list_pos(
    admin: CurrentAdmin,
    session: DbSession,
    pagination: Annotated[Pagination, Depends(get_pagination)],
    status: Annotated[DevicePoStatus | None, Query()] = None,
    q: Annotated[str | None, Query(description="Matches the PO code.")] = None,
) -> Paginated[DevicePoOut]:
    """An MPX sees its own orders, a DP sees orders placed with it, IOH sees all."""
    items, total = await service.list_pos(session, admin, status=status, q=q, pagination=pagination)
    return Paginated[DevicePoOut].model_validate(pagination.envelope(items, total))


@router.post(
    "",
    summary="Raise a device order",
    response_model=DevicePoOut,
    status_code=201,
    responses={403: {"description": "Role not permitted"}},
)
async def create_po(
    admin: CurrentAdmin, session: DbSession, payload: CreateDevicePoRequest
) -> DevicePoOut:
    """MPX only. The unit price is snapshotted from the catalogue here; a model with no
    confirmed price is refused rather than ordered at a guess."""
    return await service.create_po(session, admin, payload)


@router.get(
    "/{poId}",
    summary="One device order, with the Riwayat history and delivery address",
    response_model=DevicePoDetailOut,
    responses={404: {"description": "Not found"}},
)
async def get_po(
    admin: CurrentAdmin, session: DbSession, po_id: Annotated[uuid.UUID, Path(alias="poId")]
) -> DevicePoDetailOut:
    return await service.get_po(session, admin, po_id)


@router.get(
    "/{poId}/bundles",
    summary="The units attached to this order",
    response_model=Wrapped[list[BundleOut]],
)
async def list_bundles(
    admin: CurrentAdmin, session: DbSession, po_id: Annotated[uuid.UUID, Path(alias="poId")]
) -> Wrapped[list[BundleOut]]:
    """Backs the *Detail IMEI & MSISDN* modal."""
    return Wrapped(data=await service.list_bundles(session, admin, po_id))


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


@router.post("/{poId}/accept", summary="DP accepts the order", response_model=DevicePoOut)
async def accept_po(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: NoteRequest | None = None,
) -> DevicePoOut:
    return await service.accept_po(session, admin, po_id, note=payload.note if payload else None)


@router.post(
    "/{poId}/reject",
    summary="DP declines the order",
    response_model=DevicePoOut,
    responses={409: {"description": "Not in a rejectable state"}},
)
async def reject_po(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: RejectRequest,
) -> DevicePoOut:
    return await service.reject_po(session, admin, po_id, reason=payload.reason)


@router.post(
    "/{poId}/cancel",
    summary="MPX withdraws the order",
    response_model=DevicePoOut,
    responses={409: {"description": "The DP has already accepted it"}},
)
async def cancel_po(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: NoteRequest | None = None,
) -> DevicePoOut:
    return await service.cancel_po(session, admin, po_id, note=payload.note if payload else None)


@router.post(
    "/{poId}/bundles:validate",
    summary="Dry run of a bundle attachment",
    response_model=AttachBundlesValidationOut,
)
async def validate_bundles(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: AttachBundlesRequest,
) -> AttachBundlesValidationOut:
    return await service.validate_bundles(session, admin, po_id, msisdns=payload.msisdns)


@router.post(
    "/{poId}/bundles",
    summary="DP attaches bundles to the order",
    response_model=AttachBundlesResultOut,
    responses={422: {"description": "Count mismatch, or a bundle that is not yours"}},
)
async def attach_bundles(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: AttachBundlesRequest,
    idempotency_key: IdempotencyKey,
) -> AttachBundlesResultOut:
    """The attached count must equal the ordered quantity. All or nothing."""
    endpoint = f"POST /admin/device-pos/{po_id}/bundles"
    digest = hash_request(payload.model_dump(mode="json"))

    cached = await replay(
        session,
        admin_user_id=admin.admin_user_id,
        endpoint=endpoint,
        key=idempotency_key,
        request_hash=digest,
    )
    if cached is not None:
        return AttachBundlesResultOut.model_validate(cached)

    result = await service.attach_bundles(session, admin, po_id, msisdns=payload.msisdns)
    await remember(
        session,
        admin_user_id=admin.admin_user_id,
        endpoint=endpoint,
        key=idempotency_key,
        request_hash=digest,
        response_body=result.model_dump(mode="json", by_alias=True),
    )
    return result
