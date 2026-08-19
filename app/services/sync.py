"""Offline bootstrap for the mobile client.

Called on login and on pull-to-refresh. Returns everything the app needs to keep
working without a connection, plus the server clock so the device can correct its own
drift rather than acting on it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.periods import now
from app.models.activation import Activation
from app.models.customer import Customer
from app.models.identity import AccountExecutive
from app.schemas.sync import BootstrapResponse
from app.services.auth import to_profile
from app.services.customers import to_customer_out
from app.services.inventory import fetch_allocated_stock

# The payload has to fit on a field connection. These caps keep a first sync from
# becoming a multi-megabyte download over 3G; the app pages for anything older.
STOCK_LIMIT: Final = 500
CUSTOMER_LIMIT: Final = 500


async def bootstrap(
    session: AsyncSession,
    settings: Settings,
    *,
    ae: AccountExecutive,
    since: datetime | None,
) -> BootstrapResponse:
    filters = [Customer.ae_id == ae.ae_id]
    if since is not None:
        filters.append(or_(Customer.updated_at > since, Customer.created_at > since))

    activation_exists = exists().where(Activation.customer_id == Customer.customer_id)
    rows = await session.execute(
        select(Customer, activation_exists.label("has_activation"))
        .where(*filters)
        .order_by(Customer.updated_at.desc())
        .limit(CUSTOMER_LIMIT)
    )

    return BootstrapResponse(
        server_time=now(settings),
        profile=to_profile(ae),
        inventory=await fetch_allocated_stock(session, ae_id=ae.ae_id, limit=STOCK_LIMIT),
        customers=[to_customer_out(customer, has_activation=flag) for customer, flag in rows.all()],
    )
