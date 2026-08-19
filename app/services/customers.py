"""Prospect registration, lists, and funnel movement.

Every query in this module is scoped to the authenticated AE. A customer belonging to
another AE is reported as ``404`` and never ``403`` — a 403 would confirm the record
exists, which is exactly what a curious AE fishing through ID ranges wants to learn.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Select, String, cast, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_device_time
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.core.geo import is_verified
from app.core.pagination import Pagination
from app.core.periods import DateRange
from app.models.activation import Activation
from app.models.customer import Customer, CustomerStatusHistory
from app.models.enums import CustomerStatus
from app.schemas.common import GeoPoint
from app.schemas.customer import (
    CustomerCreate,
    CustomerDetail,
    CustomerLookupItem,
    CustomerOut,
    CustomerUpdate,
    StatusHistoryEntry,
)
from app.services.activations import to_activation_out


def to_customer_out(customer: Customer, *, has_activation: bool) -> CustomerOut:
    return CustomerOut(
        customer_id=customer.customer_id,
        full_name=customer.full_name,
        phone_number=customer.phone_number,
        address=customer.address,
        visit_date=customer.visit_date,
        status=customer.status,
        geo=GeoPoint.build(
            latitude=customer.latitude,
            longitude=customer.longitude,
            accuracy_m=customer.geo_accuracy_m,
            verified=customer.geo_verified,
            is_mocked=customer.is_mocked,
        ),
        has_activation=has_activation,
        created_at=customer.created_at,
    )


def _has_activation_column() -> Select[tuple[Customer, bool]]:
    """Customer rows plus the hasActivation flag, in one round trip rather than N+1."""
    activation_exists = (
        exists().where(Activation.customer_id == Customer.customer_id).label("has_activation")
    )
    return select(Customer, activation_exists)


async def list_customers(
    session: AsyncSession,
    *,
    ae_id: uuid.UUID,
    window: DateRange,
    statuses: list[CustomerStatus] | None,
    query: str | None,
    pagination: Pagination,
) -> tuple[list[CustomerOut], int]:
    filters = [
        Customer.ae_id == ae_id,
        Customer.visit_date >= window.start,
        Customer.visit_date <= window.end,
    ]
    if statuses:
        filters.append(Customer.status.in_(statuses))
    if query:
        pattern = f"%{query.strip()}%"
        filters.append(or_(Customer.full_name.ilike(pattern), Customer.phone_number.ilike(pattern)))

    total = await session.scalar(select(func.count()).select_from(Customer).where(*filters))

    rows = await session.execute(
        _has_activation_column()
        .where(*filters)
        .order_by(Customer.visit_date.desc(), Customer.customer_id.desc())
        .limit(pagination.limit)
        .offset(pagination.offset)
    )

    items = [to_customer_out(customer, has_activation=flag) for customer, flag in rows.all()]
    return items, int(total or 0)


async def create_customer(
    session: AsyncSession,
    settings: Settings,
    *,
    ae_id: uuid.UUID,
    payload: CustomerCreate,
    idempotency_key: uuid.UUID | None,
) -> tuple[CustomerOut, bool]:
    """Register a prospect. Returns ``(customer, created)``.

    ``created`` is False when an ``Idempotency-Key`` replay returned the original row,
    which is what makes the mobile outbox safe to retry after a dropped response.
    """
    record_device_time(ae_id=ae_id, action="customer.create", device_time=payload.device_time)

    if idempotency_key is not None:
        replayed = await _replay_customer(
            session, ae_id=ae_id, key=idempotency_key, payload=payload
        )
        if replayed is not None:
            return replayed, False

    customer = Customer(
        ae_id=ae_id,
        visit_date=payload.visit_date,
        full_name=payload.full_name.strip(),
        phone_number=payload.phone_number,
        address=payload.address.strip(),
        latitude=payload.latitude,
        longitude=payload.longitude,
        geo_accuracy_m=(
            Decimal(str(payload.geo_accuracy_m)) if payload.geo_accuracy_m is not None else None
        ),
        geo_verified=is_verified(
            settings, accuracy_m=payload.geo_accuracy_m, is_mocked=payload.is_mocked
        ),
        is_mocked=payload.is_mocked,
        status=payload.status,
        # Server time, not payload.device_time. Field phones have wrong clocks.
        visited_at=datetime.now(UTC),
        idempotency_key=idempotency_key,
    )
    session.add(customer)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise _translate_customer_conflict(exc) from exc

    session.add(
        CustomerStatusHistory(
            customer_id=customer.customer_id,
            old_status=None,
            new_status=customer.status,
            changed_by=ae_id,
            note="Registered",
        )
    )
    await session.flush()
    return to_customer_out(customer, has_activation=False), True


async def get_customer_detail(
    session: AsyncSession, *, ae_id: uuid.UUID, customer_id: int
) -> CustomerDetail:
    customer = await _get_owned(session, ae_id=ae_id, customer_id=customer_id)

    history = (
        await session.scalars(
            select(CustomerStatusHistory)
            .where(CustomerStatusHistory.customer_id == customer_id)
            .order_by(CustomerStatusHistory.changed_at.desc())
        )
    ).all()

    activations = (
        await session.scalars(
            select(Activation)
            .where(Activation.customer_id == customer_id)
            .order_by(Activation.submitted_at.desc())
        )
    ).all()

    base = to_customer_out(customer, has_activation=bool(activations))
    return CustomerDetail(
        **base.model_dump(by_alias=False),
        status_history=[
            StatusHistoryEntry(
                old_status=entry.old_status,
                new_status=entry.new_status,
                note=entry.note,
                changed_at=entry.changed_at,
            )
            for entry in history
        ],
        activations=[to_activation_out(activation) for activation in activations],
    )


async def update_customer(
    session: AsyncSession,
    *,
    ae_id: uuid.UUID,
    customer_id: int,
    payload: CustomerUpdate,
) -> CustomerDetail:
    customer = await _get_owned(session, ae_id=ae_id, customer_id=customer_id)

    if payload.full_name is not None:
        customer.full_name = payload.full_name.strip()
    if payload.phone_number is not None:
        customer.phone_number = payload.phone_number
    if payload.address is not None:
        customer.address = payload.address.strip()

    # Every status change is written to history — that is what makes the
    # Hot Leads → Purchase conversion rate measurable later.
    if payload.status is not None and payload.status != customer.status:
        session.add(
            CustomerStatusHistory(
                customer_id=customer.customer_id,
                old_status=customer.status,
                new_status=payload.status,
                changed_by=ae_id,
                note=payload.status_note,
            )
        )
        customer.status = payload.status

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise _translate_customer_conflict(exc) from exc

    return await get_customer_detail(session, ae_id=ae_id, customer_id=customer_id)


async def lookup_customers(
    session: AsyncSession,
    *,
    ae_id: uuid.UUID,
    query: str | None,
    exclude_activated: bool,
    limit: int,
) -> list[CustomerLookupItem]:
    """Typeahead source for the activation form's "ID Customer" dropdown."""
    statement = select(Customer.customer_id, Customer.full_name).where(Customer.ae_id == ae_id)

    if query:
        pattern = f"%{query.strip()}%"
        statement = statement.where(
            or_(
                Customer.full_name.ilike(pattern),
                Customer.phone_number.ilike(pattern),
                # The dropdown shows "1234567890 - Susanto", so an AE typing digits is
                # plausibly searching by the ID they can see.
                cast(Customer.customer_id, String).ilike(pattern),
            )
        )
    if exclude_activated:
        statement = statement.where(~exists().where(Activation.customer_id == Customer.customer_id))

    rows = await session.execute(
        statement.order_by(Customer.visit_date.desc(), Customer.customer_id.desc()).limit(limit)
    )
    return [
        CustomerLookupItem(
            customer_id=customer_id,
            full_name=full_name,
            label=f"{customer_id} - {full_name}",
        )
        for customer_id, full_name in rows.all()
    ]


async def _get_owned(session: AsyncSession, *, ae_id: uuid.UUID, customer_id: int) -> Customer:
    customer = await session.scalar(
        select(Customer).where(Customer.customer_id == customer_id, Customer.ae_id == ae_id)
    )
    if customer is None:
        raise NotFoundError("Customer not found.", code="CUSTOMER_NOT_FOUND")
    return customer


async def _replay_customer(
    session: AsyncSession,
    *,
    ae_id: uuid.UUID,
    key: uuid.UUID,
    payload: CustomerCreate,
) -> CustomerOut | None:
    """Return the original resource if this key was already used with the same payload.

    There is no stored request hash, so the comparison is against the persisted row —
    which is the same thing, and avoids a schema column that would exist only to
    remember something the row already knows.
    """
    existing = await session.scalar(select(Customer).where(Customer.idempotency_key == key))
    if existing is None:
        return None

    same_request = (
        existing.ae_id == ae_id
        and existing.visit_date == payload.visit_date
        and existing.full_name == payload.full_name.strip()
        and existing.phone_number == payload.phone_number
        and existing.address == payload.address.strip()
        and existing.status == payload.status
    )
    if not same_request:
        raise ConflictError(
            "This Idempotency-Key was already used with a different request.",
            code="IDEMPOTENCY_KEY_REUSED",
        )

    has_activation = bool(
        await session.scalar(
            select(Activation.activation_id).where(Activation.customer_id == existing.customer_id)
        )
    )
    return to_customer_out(existing, has_activation=has_activation)


def _translate_customer_conflict(exc: IntegrityError) -> ConflictError:
    constraint = str(getattr(exc.orig, "constraint_name", "") or exc.orig or "")
    if "uq_cust_ae_phone" in constraint:
        return ConflictError(
            "You have already registered a customer with this phone number.",
            code="CUSTOMER_ALREADY_REGISTERED",
            details=[{"field": "phoneNumber", "issue": "already registered by this AE"}],
        )
    if "uq_cust_idem" in constraint:
        return ConflictError(
            "This Idempotency-Key was already used with a different request.",
            code="IDEMPOTENCY_KEY_REUSED",
        )
    return ConflictError("The customer could not be saved.", code="CUSTOMER_CONFLICT")
