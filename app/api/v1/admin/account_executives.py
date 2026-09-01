"""The AE directory — MPX only.

`GET /admin/account-executives` is the endpoint that could not exist on the AE
audience. Golden rule 2 scopes every AE-facing list to the token, so there is no AE
route that returns more than one AE, and adding a parameter to one would have broken
the rule. Here the scoping comes from the caller's own MPX binding instead.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.core.deps import CurrentAdmin, DbSession
from app.core.pagination import Pagination, get_pagination
from app.schemas.account_executive import (
    AdminAccountExecutiveDetailOut,
    AdminAccountExecutiveOut,
)
from app.schemas.common import Paginated
from app.services import account_executives as service

router = APIRouter(prefix="/account-executives", tags=["Account Executives"])


@router.get(
    "",
    summary="Account Executives drawing stock from this MPX",
    response_model=Paginated[AdminAccountExecutiveOut],
    responses={403: {"description": "Role not permitted"}},
)
async def list_account_executives(
    admin: CurrentAdmin,
    session: DbSession,
    pagination: Annotated[Pagination, Depends(get_pagination)],
    q: Annotated[str | None, Query(description="Matches AE code or full name.")] = None,
) -> Paginated[AdminAccountExecutiveOut]:
    """Backs the *Nama AE* dropdown on the stock allocation modal."""
    items, total = await service.list_aes(session, admin, q=q, pagination=pagination)
    return Paginated[AdminAccountExecutiveOut].model_validate(pagination.envelope(items, total))


@router.get(
    "/{aeId}",
    summary="One AE, with what they are currently holding",
    response_model=AdminAccountExecutiveDetailOut,
    responses={403: {"description": "Role not permitted"}, 404: {"description": "Not found"}},
)
async def get_account_executive(
    admin: CurrentAdmin,
    session: DbSession,
    ae_id: Annotated[uuid.UUID, Path(alias="aeId")],
) -> AdminAccountExecutiveDetailOut:
    """Backs the *Tentang AE* panel. An AE drawing stock from another MPX reads as
    absent rather than forbidden."""
    return await service.get_ae(session, admin, ae_id)
