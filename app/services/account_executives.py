"""The AE directory, as an MPX sees it.

This is the clearest example of why the dashboard needed a separate token audience.
Golden rule 2 hard-scopes every AE-facing list to the caller, so the deployed API has
``GET /me`` and no route that returns more than one AE — adding a query parameter to an
AE endpoint would have violated the rule outright. An MPX legitimately needs the list to
decide who to allocate stock to, so it lives here, on an admin-audience route, scoped by
the caller's own MPX binding rather than by anything the client sends.

The join is on ``account_executive.mpx_code = mpx.code``. That is a string match rather
than a foreign key because ``mpx_code`` has been free text on a live table since the
initial schema, and the codes already in it (``MPX-BKL-01``, ``MPX-SDA-02``) are exactly
the format ratified for ``mpx.code``. Converting the column to an FK would mean touching
the AE table for no behaviour a match on an indexed unique code does not already give.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError
from app.core.pagination import Pagination
from app.models.admin import AdminUser
from app.models.enums import AdminRole, InventoryStatus
from app.models.identity import AccountExecutive
from app.models.inventory import DeviceModel, FwaInventory
from app.schemas.account_executive import (
    AdminAccountExecutiveDetailOut,
    AdminAccountExecutiveOut,
    AllocatedStockLine,
)
from app.schemas.auth import RegionOut


def _require_mpx(admin: AdminUser) -> str:
    """An MPX admin, and the code its AEs are matched against."""
    if admin.role is not AdminRole.MPX_ADMIN or admin.mpx is None:
        raise ForbiddenError(
            "Only an MPX can browse its Account Executives.", code="ROLE_NOT_PERMITTED"
        )
    return admin.mpx.code


def _to_out(ae: AccountExecutive) -> AdminAccountExecutiveOut:
    return AdminAccountExecutiveOut(
        ae_id=ae.ae_id,
        ae_code=ae.ae_code,
        full_name=ae.full_name,
        brand_scope=ae.brand_scope,
        region=(
            RegionOut(region_code=ae.region.region_code, region_name=ae.region.region_name)
            if ae.region is not None
            else None
        ),
        mpx_code=ae.mpx_code,
        status=ae.status,
    )


async def list_aes(
    session: AsyncSession,
    admin: AdminUser,
    *,
    q: str | None,
    pagination: Pagination,
) -> tuple[list[AdminAccountExecutiveOut], int]:
    """Backs the *Nama AE* dropdown on the stock allocation modal."""
    mpx_code = _require_mpx(admin)

    filters = [AccountExecutive.mpx_code == mpx_code]
    if q:
        term = f"%{q.strip()}%"
        filters.append(
            AccountExecutive.ae_code.ilike(term) | AccountExecutive.full_name.ilike(term)
        )

    total = int(
        await session.scalar(select(func.count()).select_from(AccountExecutive).where(*filters))
        or 0
    )
    rows = (
        await session.scalars(
            select(AccountExecutive)
            .where(*filters)
            .order_by(AccountExecutive.ae_code)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
    ).all()
    return [_to_out(row) for row in rows], total


async def get_ae(
    session: AsyncSession, admin: AdminUser, ae_id: uuid.UUID
) -> AdminAccountExecutiveDetailOut:
    """Backs the *Tentang AE* panel: ID, Brand, Branch, and what they are holding."""
    mpx_code = _require_mpx(admin)

    ae = await session.scalar(
        select(AccountExecutive).where(
            AccountExecutive.ae_id == ae_id,
            # An AE belonging to another stock point reads as absent, not forbidden.
            AccountExecutive.mpx_code == mpx_code,
        )
    )
    if ae is None:
        raise NotFoundError("This Account Executive does not exist.", code="AE_NOT_FOUND")

    # What the AE is currently holding, grouped for the panel. ACTIVATED units have left
    # their stock and are deliberately excluded; CONSUMED is a unit sold but not yet
    # confirmed by the GA feed, which is still off the shelf.
    rows = (
        await session.execute(
            select(DeviceModel.model_code, func.count())
            .select_from(FwaInventory)
            .outerjoin(DeviceModel, DeviceModel.device_model_id == FwaInventory.device_model_id)
            .where(
                FwaInventory.allocated_ae_id == ae.ae_id,
                FwaInventory.status == InventoryStatus.ALLOCATED,
            )
            .group_by(DeviceModel.model_code)
            .order_by(DeviceModel.model_code)
        )
    ).all()

    lines = [
        AllocatedStockLine(device_model_code=model_code, count=count) for model_code, count in rows
    ]
    base = _to_out(ae)
    return AdminAccountExecutiveDetailOut(
        **base.model_dump(),
        allocated_stock=lines,
        allocated_total=sum(line.count for line in lines),
    )
