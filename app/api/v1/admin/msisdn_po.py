"""MSISDN PO and hard-bundle pairing endpoints — steps 5 and 6.

Note what is absent: no route accepts a `status`, a `poCode`, or a `devicePartnerId`.
Status moves only through the named actions below (golden rule 8), codes are generated
server-side, and the organisation comes from the token (golden rule 7).
"""

from __future__ import annotations

import csv
import io
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from app.core.deps import CurrentAdmin, DbSession, IdempotencyKey
from app.core.pagination import Pagination, get_pagination
from app.models.enums import MsisdnPoStatus
from app.schemas.common import Paginated, Wrapped
from app.schemas.purchasing import (
    CreateMsisdnPoRequest,
    MsisdnOut,
    MsisdnPoDetailOut,
    MsisdnPoOut,
    NoteRequest,
    PairingRequest,
    PairingResultOut,
    PairingValidationOut,
    RejectRequest,
    SupplyRequest,
    SupplyResultOut,
    SupplyValidationOut,
)
from app.services import msisdn_po as service
from app.services.supply_common import hash_request, remember, replay

router = APIRouter(prefix="/msisdn-pos", tags=["MSISDN PO"])


@router.get("", summary="List MSISDN requests", response_model=Paginated[MsisdnPoOut])
async def list_pos(
    admin: CurrentAdmin,
    session: DbSession,
    pagination: Annotated[Pagination, Depends(get_pagination)],
    status: Annotated[MsisdnPoStatus | None, Query()] = None,
    q: Annotated[str | None, Query(description="Matches the PO code.")] = None,
) -> Paginated[MsisdnPoOut]:
    """A Device Partner sees only its own requests; IOH sees every partner's."""
    items, total = await service.list_pos(session, admin, status=status, q=q, pagination=pagination)
    return Paginated[MsisdnPoOut].model_validate(pagination.envelope(items, total))


@router.post(
    "",
    summary="Raise an MSISDN request",
    response_model=MsisdnPoOut,
    status_code=201,
    responses={403: {"description": "Role not permitted"}},
)
async def create_po(
    admin: CurrentAdmin,
    session: DbSession,
    payload: CreateMsisdnPoRequest,
) -> MsisdnPoOut:
    """DP only. The PO code is generated here and is never accepted from the client."""
    return await service.create_po(session, admin, payload)


@router.get(
    "/{poId}",
    summary="One MSISDN request, with its status history",
    response_model=MsisdnPoDetailOut,
    responses={404: {"description": "Not found"}},
)
async def get_po(
    admin: CurrentAdmin, session: DbSession, po_id: Annotated[uuid.UUID, Path(alias="poId")]
) -> MsisdnPoDetailOut:
    return await service.get_po(session, admin, po_id)


@router.get(
    "/{poId}/msisdns",
    summary="The numbers issued against this request",
    # Two media types from one route, so the response model cannot be inferred from the
    # return annotation. The contract documents both.
    response_model=None,
    responses={
        200: {"description": "JSON, or CSV when format=csv"},
        404: {"description": "Not found"},
    },
)
async def list_msisdns(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    fmt: Annotated[str, Query(alias="format", pattern="^(json|csv)$")] = "json",
) -> Response | Wrapped[list[MsisdnOut]]:
    """Backs the download icon on a supplied PO.

    `xlsx` is deliberately not offered: it would mean adding a spreadsheet dependency,
    and this project does not add one without asking. CSV opens in Excel.
    """
    rows = await service.list_msisdns(session, admin, po_id)
    if fmt == "json":
        return Wrapped(data=rows)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["msisdn", "imei", "status", "paired_at"])
    for row in rows:
        writer.writerow(
            [
                row.msisdn,
                row.imei or "",
                row.status,
                row.paired_at.isoformat() if row.paired_at else "",
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="msisdns.csv"'},
    )


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


@router.post("/{poId}/process", summary="IOH picks the request up", response_model=MsisdnPoOut)
async def process_po(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: NoteRequest | None = None,
) -> MsisdnPoOut:
    return await service.process_po(session, admin, po_id, note=payload.note if payload else None)


@router.post(
    "/{poId}/reject",
    summary="IOH declines the request",
    response_model=MsisdnPoOut,
    responses={409: {"description": "Not in a rejectable state"}},
)
async def reject_po(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: RejectRequest,
) -> MsisdnPoOut:
    """The reason is mandatory. It surfaces on the DP list so they can resubmit."""
    return await service.reject_po(session, admin, po_id, reason=payload.reason)


@router.post(
    "/{poId}/cancel",
    summary="DP withdraws the request",
    response_model=MsisdnPoOut,
    responses={409: {"description": "IOH has already acted on it"}},
)
async def cancel_po(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: NoteRequest | None = None,
) -> MsisdnPoOut:
    return await service.cancel_po(session, admin, po_id, note=payload.note if payload else None)


@router.post(
    "/{poId}/supply:validate",
    summary="Dry run of an MSISDN import",
    response_model=SupplyValidationOut,
)
async def validate_supply(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: SupplyRequest,
) -> SupplyValidationOut:
    """Writes nothing. The operator must see 400 parsed numbers before committing 400."""
    return await service.validate_supply(session, admin, po_id, msisdns=payload.msisdns)


@router.post(
    "/{poId}/supply",
    summary="IOH supplies the numbers",
    response_model=SupplyResultOut,
    responses={409: {"description": "Not in a suppliable state"}},
)
async def supply(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: SupplyRequest,
    idempotency_key: IdempotencyKey,
) -> SupplyResultOut:
    """One transaction, all or nothing, and safe to retry.

    A dropped response on a 400-row import must not insert 400 more numbers on retry,
    so the original result is replayed from `idempotency_record`.
    """
    endpoint = f"POST /admin/msisdn-pos/{po_id}/supply"
    digest = hash_request(payload.model_dump(mode="json"))

    cached = await replay(
        session,
        admin_user_id=admin.admin_user_id,
        endpoint=endpoint,
        key=idempotency_key,
        request_hash=digest,
    )
    if cached is not None:
        return SupplyResultOut.model_validate(cached)

    result = await service.supply(session, admin, po_id, msisdns=payload.msisdns)
    await remember(
        session,
        admin_user_id=admin.admin_user_id,
        endpoint=endpoint,
        key=idempotency_key,
        request_hash=digest,
        response_body=result.model_dump(mode="json", by_alias=True),
    )
    return result


# ---------------------------------------------------------------------------
# Hard-bundle pairing — step 6
# ---------------------------------------------------------------------------


@router.get(
    "/{poId}/pairing",
    summary="Numbers on this PO still awaiting an IMEI",
    response_model=Wrapped[list[MsisdnOut]],
)
async def get_pairing(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
) -> Wrapped[list[MsisdnOut]]:
    rows = await service.list_msisdns(session, admin, po_id)
    return Wrapped(data=[row for row in rows if row.imei is None])


@router.post(
    "/{poId}/pairing:validate",
    summary="Dry run of an IMEI pairing batch",
    response_model=PairingValidationOut,
)
async def validate_pairing(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: PairingRequest,
) -> PairingValidationOut:
    return await service.validate_pairing(session, admin, po_id, pairs=payload.pairs)


@router.post(
    "/{poId}/pairing",
    summary="Pair an IMEI to every number on this PO",
    response_model=PairingResultOut,
    responses={422: {"description": "Count mismatch or a rejected row"}},
)
async def pair(
    admin: CurrentAdmin,
    session: DbSession,
    po_id: Annotated[uuid.UUID, Path(alias="poId")],
    payload: PairingRequest,
    idempotency_key: IdempotencyKey,
) -> PairingResultOut:
    """The IMEI count must equal the MSISDN count. A partial pairing is rejected, not
    applied — half a bundle is a unit nobody can ship and nobody can find."""
    endpoint = f"POST /admin/msisdn-pos/{po_id}/pairing"
    digest = hash_request(payload.model_dump(mode="json"))

    cached = await replay(
        session,
        admin_user_id=admin.admin_user_id,
        endpoint=endpoint,
        key=idempotency_key,
        request_hash=digest,
    )
    if cached is not None:
        return PairingResultOut.model_validate(cached)

    result = await service.pair(session, admin, po_id, pairs=payload.pairs)
    await remember(
        session,
        admin_user_id=admin.admin_user_id,
        endpoint=endpoint,
        key=idempotency_key,
        request_hash=digest,
        response_body=result.model_dump(mode="json", by_alias=True),
    )
    return result
