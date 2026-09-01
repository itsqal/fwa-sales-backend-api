"""Reference-data endpoints for the dashboard.

Two of these are role-restricted, and the reasoning is worth stating because it is not
symmetric with the AE side. On AE routes a forbidden record returns 404, never 403,
because a 403 would confirm the record exists. Here 403 is correct: the dashboard
sidebar already tells a Device Partner that MPX screens exist, so refusing by name
leaks nothing that the UI does not already show.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.deps import CurrentAdmin, DbSession, require_roles
from app.models.enums import AdminRole
from app.schemas.common import Wrapped
from app.schemas.reference import (
    BrandOut,
    CallPlanOut,
    DeviceModelRefOut,
    DevicePartnerRefOut,
    MpxRefOut,
)
from app.services import reference as reference_service

router = APIRouter(prefix="/reference", tags=["Admin Reference"])


@router.get(
    "/call-plans",
    summary="Call plans available on an MSISDN request",
    response_model=Wrapped[list[CallPlanOut]],
)
async def list_call_plans(_: CurrentAdmin, session: DbSession) -> Wrapped[list[CallPlanOut]]:
    """Backs the *Call Plan* dropdown.

    Branch on `kind`, not on the absence of `quotaGb`: *Saldo Mobo* is a balance
    top-up rather than a data bundle, and the database enforces that pairing.
    """
    return Wrapped(data=await reference_service.list_call_plans(session))


@router.get(
    "/brands",
    summary="Telco retail brands",
    response_model=Wrapped[list[BrandOut]],
)
async def list_brands(_: CurrentAdmin, session: DbSession) -> Wrapped[list[BrandOut]]:
    return Wrapped(data=await reference_service.list_brands(session))


@router.get(
    "/device-models",
    summary="Device catalogue",
    response_model=Wrapped[list[DeviceModelRefOut]],
)
async def list_device_models(
    _: CurrentAdmin,
    session: DbSession,
    include_inactive: Annotated[bool, Query(alias="includeInactive")] = False,
) -> Wrapped[list[DeviceModelRefOut]]:
    """Backs the device picker on the MPX *Buat PO* form.

    A `listPriceIdr` of null means the model has no confirmed price and cannot be
    ordered yet. The picker should surface that rather than hide the model, so the
    gap is visible to whoever can fix it.
    """
    return Wrapped(
        data=await reference_service.list_device_models(session, include_inactive=include_inactive)
    )


@router.get(
    "/device-partners",
    summary="Device Partners",
    response_model=Wrapped[list[DevicePartnerRefOut]],
    dependencies=[Depends(require_roles(AdminRole.IOH_ADMIN, AdminRole.MPX_ADMIN))],
    responses={403: {"description": "Role not permitted"}},
)
async def list_device_partners(session: DbSession) -> Wrapped[list[DevicePartnerRefOut]]:
    """IOH supplies them and MPX orders from them. A Device Partner has no need to
    enumerate its competitors."""
    return Wrapped(data=await reference_service.list_device_partners(session))


@router.get(
    "/mpx",
    summary="MPX stock points",
    response_model=Wrapped[list[MpxRefOut]],
    dependencies=[Depends(require_roles(AdminRole.IOH_ADMIN, AdminRole.DP_ADMIN))],
    responses={403: {"description": "Role not permitted"}},
)
async def list_mpx(session: DbSession) -> Wrapped[list[MpxRefOut]]:
    """An MPX admin already knows which MPX it is — that is on `GET /admin/me`."""
    return Wrapped(data=await reference_service.list_mpx(session))
