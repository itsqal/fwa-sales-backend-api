"""Customer activation — the AE's "Sell In".

This is the write path that matters most. The client sends a customer, a scanned
MSISDN and a GPS fix; everything else about the device is read from ``fwa_inventory``
under a row lock and snapshotted onto the activation. The row is created
``NOT_ACTIVATED``: only the Gross Add feed from GCP may stamp ``activation_date`` and
turn the badge green, because that stamp is what gates incentive.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record_device_time
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.geo import is_verified
from app.core.pagination import Pagination
from app.core.periods import DateRange
from app.models.activation import Activation
from app.models.customer import Customer
from app.models.enums import ActivationStatus, IneligibilityReason, InventoryStatus
from app.models.inventory import FwaInventory
from app.schemas.activation import ActivationCreate, ActivationCustomerOut, ActivationOut
from app.schemas.common import GeoPoint
from app.services.inventory import assess_eligibility

_INELIGIBILITY_ERRORS: dict[IneligibilityReason, tuple[int, str, str]] = {
    IneligibilityReason.NOT_ALLOCATED_TO_YOU: (
        422,
        "MSISDN_NOT_ALLOCATED",
        "This unit is not in your stock.",
    ),
    IneligibilityReason.ALREADY_ACTIVATED: (
        409,
        "MSISDN_ALREADY_ACTIVATED",
        "This unit has already been activated.",
    ),
    IneligibilityReason.BLOCKED: (422, "MSISDN_BLOCKED", "This unit is blocked."),
    IneligibilityReason.RETURNED: (
        422,
        "MSISDN_RETURNED",
        "This unit has been returned to the stock point.",
    ),
}


def to_activation_out(activation: Activation) -> ActivationOut:
    return ActivationOut(
        activation_id=activation.activation_id,
        customer=ActivationCustomerOut(
            customer_id=activation.customer.customer_id,
            full_name=activation.customer.full_name,
        ),
        msisdn=activation.msisdn,
        imei=activation.imei,
        device_model_code=activation.device_model_code,
        geo=GeoPoint.build(
            latitude=activation.latitude,
            longitude=activation.longitude,
            accuracy_m=activation.geo_accuracy_m,
            verified=activation.geo_verified,
            is_mocked=activation.is_mocked,
        ),
        submitted_at=activation.submitted_at,
        status=activation.status,
        activation_date=activation.activation_date,
    )


async def create_activation(
    session: AsyncSession,
    settings: Settings,
    *,
    ae_id: uuid.UUID,
    payload: ActivationCreate,
    idempotency_key: uuid.UUID | None,
) -> tuple[ActivationOut, bool]:
    """Submit an activation. Returns ``(activation, created)``."""
    record_device_time(ae_id=ae_id, action="activation.create", device_time=payload.device_time)

    if idempotency_key is not None:
        replayed = await _replay_activation(
            session, ae_id=ae_id, key=idempotency_key, payload=payload
        )
        if replayed is not None:
            return replayed, False

    customer = await session.scalar(
        select(Customer).where(Customer.customer_id == payload.customer_id, Customer.ae_id == ae_id)
    )
    if customer is None:
        # The contract declares 422 CUSTOMER_NOT_OWNED for this path specifically,
        # rather than the 404 that AE-scoped reads use.
        raise ValidationFailedError(
            "That customer is not one of yours.",
            code="CUSTOMER_NOT_OWNED",
            details=[{"field": "customerId", "issue": "not registered by this AE"}],
        )

    # Lock the unit for the rest of the transaction. Two devices submitting the same
    # scanned box is the exact race this guards; the unique constraint on
    # activation.msisdn is the backstop if the lock is somehow bypassed.
    # `of=FwaInventory` matters: the model eager-loads device_model with a LEFT JOIN,
    # and PostgreSQL refuses FOR UPDATE on the nullable side of an outer join. We only
    # want the unit row locked anyway.
    item = await session.scalar(
        select(FwaInventory)
        .where(FwaInventory.msisdn == payload.msisdn)
        .with_for_update(of=FwaInventory)
    )
    if item is None:
        raise ValidationFailedError(
            "This number is not a registered HiFi AIR unit.",
            code="MSISDN_NOT_FOUND",
            details=[{"field": "msisdn", "issue": "not present in inventory"}],
        )

    already_activated = bool(
        await session.scalar(
            select(Activation.activation_id).where(Activation.msisdn == payload.msisdn)
        )
    )

    reason = assess_eligibility(item, ae_id=ae_id, already_activated=already_activated)
    if reason is not None:
        status_code, code, message = _INELIGIBILITY_ERRORS[reason]
        error_type = ConflictError if status_code == 409 else ValidationFailedError
        raise error_type(message, code=code, details=[{"field": "msisdn", "issue": code}])

    activation = Activation(
        ae_id=ae_id,
        customer_id=customer.customer_id,
        msisdn=item.msisdn,
        # Snapshotted from inventory, never from the request body.
        imei=item.imei,
        device_model_code=item.device_model.model_code if item.device_model else None,
        latitude=payload.latitude,
        longitude=payload.longitude,
        geo_accuracy_m=(
            Decimal(str(payload.geo_accuracy_m)) if payload.geo_accuracy_m is not None else None
        ),
        geo_verified=is_verified(
            settings, accuracy_m=payload.geo_accuracy_m, is_mocked=payload.is_mocked
        ),
        is_mocked=payload.is_mocked,
        submitted_at=datetime.now(UTC),
        status=ActivationStatus.NOT_ACTIVATED,
        idempotency_key=idempotency_key,
    )
    session.add(activation)

    # The unit leaves the AE's sellable stock now. It becomes ACTIVATED only when the
    # GA feed confirms the SIM went live.
    item.status = InventoryStatus.CONSUMED

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise _translate_activation_conflict(exc) from exc

    activation.customer = customer
    return to_activation_out(activation), True


async def list_activations(
    session: AsyncSession,
    *,
    ae_id: uuid.UUID,
    window: DateRange,
    statuses: list[ActivationStatus] | None,
    pagination: Pagination,
) -> tuple[list[ActivationOut], int]:
    # The cast resolves in the connection's timezone, which session.py pins to the
    # business zone so this list and the home chart agree on day boundaries.
    submitted_on = func.date(Activation.submitted_at)
    filters = [
        Activation.ae_id == ae_id,
        submitted_on >= window.start,
        submitted_on <= window.end,
    ]
    if statuses:
        filters.append(Activation.status.in_(statuses))

    total = await session.scalar(select(func.count()).select_from(Activation).where(*filters))

    activations = (
        await session.scalars(
            select(Activation)
            .where(*filters)
            .order_by(Activation.submitted_at.desc())
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
    ).all()

    return [to_activation_out(activation) for activation in activations], int(total or 0)


async def get_activation(
    session: AsyncSession, *, ae_id: uuid.UUID, activation_id: int
) -> ActivationOut:
    activation = await session.scalar(
        select(Activation).where(
            Activation.activation_id == activation_id, Activation.ae_id == ae_id
        )
    )
    if activation is None:
        raise NotFoundError("Activation not found.", code="ACTIVATION_NOT_FOUND")
    return to_activation_out(activation)


async def _replay_activation(
    session: AsyncSession,
    *,
    ae_id: uuid.UUID,
    key: uuid.UUID,
    payload: ActivationCreate,
) -> ActivationOut | None:
    existing = await session.scalar(select(Activation).where(Activation.idempotency_key == key))
    if existing is None:
        return None

    same_request = (
        existing.ae_id == ae_id
        and existing.customer_id == payload.customer_id
        and existing.msisdn == payload.msisdn
    )
    if not same_request:
        raise ConflictError(
            "This Idempotency-Key was already used with a different request.",
            code="IDEMPOTENCY_KEY_REUSED",
        )
    return to_activation_out(existing)


def _translate_activation_conflict(exc: IntegrityError) -> ConflictError:
    constraint = str(getattr(exc.orig, "constraint_name", "") or exc.orig or "")
    if "uq_act_msisdn" in constraint:
        return ConflictError(
            "This unit has already been activated.",
            code="MSISDN_ALREADY_ACTIVATED",
            details=[{"field": "msisdn", "issue": "already activated"}],
        )
    if "uq_act_idem" in constraint:
        return ConflictError(
            "This Idempotency-Key was already used with a different request.",
            code="IDEMPOTENCY_KEY_REUSED",
        )
    return ConflictError("The activation could not be saved.", code="ACTIVATION_CONFLICT")
