"""Reference-data reads for the dashboard.

Every list here is global by design — a call plan or a device model is the same fact
for all three roles. Golden rule 7 (org scoping comes from the token) is therefore
satisfied vacuously rather than ignored: these queries have nothing to scope. Where a
list *is* sensitive, the restriction is on the role at the router, not a filter here —
a Device Partner has no business enumerating its competitors at all, so it is refused
the endpoint rather than served a narrowed version of it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.commercial import Brand, CallPlan
from app.models.inventory import DeviceModel
from app.models.organisation import DevicePartner, Mpx
from app.schemas.reference import (
    BrandOut,
    CallPlanOut,
    DeviceModelRefOut,
    DevicePartnerRefOut,
    MpxRefOut,
)


async def list_call_plans(session: AsyncSession) -> list[CallPlanOut]:
    rows = (
        await session.scalars(
            select(CallPlan)
            .where(CallPlan.is_active.is_(True))
            .order_by(CallPlan.sort_order, CallPlan.name)
        )
    ).all()
    return [
        CallPlanOut(
            call_plan_id=row.call_plan_id,
            code=row.code,
            name=row.name,
            kind=row.kind,
            quota_gb=row.quota_gb,
            sort_order=row.sort_order,
        )
        for row in rows
    ]


async def list_brands(session: AsyncSession) -> list[BrandOut]:
    rows = (
        await session.scalars(
            select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.sort_order, Brand.code)
        )
    ).all()
    return [
        BrandOut(
            code=row.code,
            display_name=row.display_name,
            outlet_name=row.outlet_name,
            sort_order=row.sort_order,
        )
        for row in rows
    ]


async def list_device_partners(session: AsyncSession) -> list[DevicePartnerRefOut]:
    rows = (
        await session.scalars(
            select(DevicePartner)
            .where(DevicePartner.status == "ACTIVE")
            .order_by(DevicePartner.code)
        )
    ).all()
    return [
        DevicePartnerRefOut(device_partner_id=row.device_partner_id, code=row.code, name=row.name)
        for row in rows
    ]


async def list_mpx(session: AsyncSession) -> list[MpxRefOut]:
    rows = (
        await session.scalars(select(Mpx).where(Mpx.status == "ACTIVE").order_by(Mpx.code))
    ).all()
    return [
        MpxRefOut(
            mpx_id=row.mpx_id,
            code=row.code,
            name=row.name,
            legal_name=row.legal_name,
            circle=row.circle,
            region_code=row.region.region_code if row.region is not None else None,
        )
        for row in rows
    ]


async def list_device_models(
    session: AsyncSession, *, include_inactive: bool
) -> list[DeviceModelRefOut]:
    statement = select(DeviceModel).order_by(DeviceModel.model_code)
    if not include_inactive:
        statement = statement.where(DeviceModel.is_active.is_(True))

    rows = (await session.scalars(statement)).all()

    # One query for the partners rather than a lazy load per model. The catalogue is
    # three rows today, but this is the endpoint a device picker calls on every render.
    partner_ids = {row.device_partner_id for row in rows if row.device_partner_id is not None}
    partners = {
        partner.device_partner_id: partner
        for partner in (
            await session.scalars(
                select(DevicePartner).where(DevicePartner.device_partner_id.in_(partner_ids))
            )
        ).all()
    }

    return [
        DeviceModelRefOut(
            device_model_id=row.device_model_id,
            model_code=row.model_code,
            brand=row.brand,
            sku=row.sku,
            network_generation=row.network_generation,
            device_partner=(
                DevicePartnerRefOut(
                    device_partner_id=partners[row.device_partner_id].device_partner_id,
                    code=partners[row.device_partner_id].code,
                    name=partners[row.device_partner_id].name,
                )
                if row.device_partner_id in partners
                else None
            ),
            list_price_idr=row.list_price_idr,
            image_url=row.image_url,
            is_active=row.is_active,
        )
        for row in rows
    ]
