"""The MPX *Alamat Penerima* book.

Small, but it is what makes a device PO possible: `addressId` is required at order
creation and must belong to the calling MPX.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from sqlalchemy import func, select

from app.core.deps import CurrentAdmin, DbSession
from app.core.errors import ForbiddenError, NotFoundError
from app.core.pagination import Pagination, get_pagination
from app.models.commercial import Address
from app.models.enums import AdminRole
from app.schemas.address import CreateAddressRequest
from app.schemas.common import Paginated
from app.schemas.purchasing import AddressOut

router = APIRouter(prefix="/addresses", tags=["Addresses"])


def _require_mpx(admin: CurrentAdmin) -> uuid.UUID:
    if admin.role is not AdminRole.MPX_ADMIN or admin.mpx_id is None:
        raise ForbiddenError(
            "Only an MPX keeps a delivery address book.", code="ROLE_NOT_PERMITTED"
        )
    return admin.mpx_id


def _to_out(address: Address) -> AddressOut:
    return AddressOut(
        address_id=address.address_id,
        label=address.label,
        recipient_name=address.recipient_name,
        recipient_phone=address.recipient_phone,
        line1=address.line1,
        kelurahan=address.kelurahan,
        kecamatan=address.kecamatan,
        city=address.city,
        province=address.province,
        postal_code=address.postal_code,
        latitude=address.latitude,
        longitude=address.longitude,
        gmaps_url=address.gmaps_url,
        is_default=address.is_default,
    )


@router.get("", summary="Delivery addresses for this MPX", response_model=Paginated[AddressOut])
async def list_addresses(
    admin: CurrentAdmin,
    session: DbSession,
    pagination: Annotated[Pagination, Depends(get_pagination)],
) -> Paginated[AddressOut]:
    mpx_id = _require_mpx(admin)
    filters = [Address.mpx_id == mpx_id, Address.is_active.is_(True)]

    total = int(
        await session.scalar(select(func.count()).select_from(Address).where(*filters)) or 0
    )
    rows = (
        await session.scalars(
            select(Address)
            .where(*filters)
            # Default first, then alphabetical: the form preselects the default.
            .order_by(Address.is_default.desc(), Address.label)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
    ).all()
    return Paginated[AddressOut].model_validate(
        pagination.envelope([_to_out(row) for row in rows], total)
    )


@router.post("", summary="Add a delivery address", response_model=AddressOut, status_code=201)
async def create_address(
    admin: CurrentAdmin, session: DbSession, payload: CreateAddressRequest
) -> AddressOut:
    """Backs *Tambah baru* on the device PO form.

    Setting a new default clears the old one in the same transaction — a partial unique
    index enforces one default per MPX, so two concurrent attempts cannot both win.
    """
    mpx_id = _require_mpx(admin)

    if payload.is_default:
        current = await session.scalars(
            select(Address).where(Address.mpx_id == mpx_id, Address.is_default.is_(True))
        )
        for row in current:
            row.is_default = False
        await session.flush()

    address = Address(
        mpx_id=mpx_id,
        label=payload.label,
        recipient_name=payload.recipient_name,
        recipient_phone=payload.recipient_phone,
        line1=payload.line1,
        kelurahan=payload.kelurahan,
        kecamatan=payload.kecamatan,
        city=payload.city,
        province=payload.province,
        postal_code=payload.postal_code,
        latitude=payload.latitude,
        longitude=payload.longitude,
        gmaps_url=payload.gmaps_url,
        is_default=payload.is_default,
    )
    session.add(address)
    await session.flush()
    return _to_out(address)


@router.get(
    "/{addressId}",
    summary="One address, in full",
    response_model=AddressOut,
    responses={404: {"description": "Not found"}},
)
async def get_address(
    admin: CurrentAdmin,
    session: DbSession,
    address_id: Annotated[uuid.UUID, Path(alias="addressId")],
) -> AddressOut:
    """Backs the *Alamat Lengkap* modal. Another MPX's address reads as absent."""
    mpx_id = _require_mpx(admin)
    address = await session.scalar(
        select(Address).where(Address.address_id == address_id, Address.mpx_id == mpx_id)
    )
    if address is None:
        raise NotFoundError("This address does not exist.", code="ADDRESS_NOT_FOUND")
    return _to_out(address)
