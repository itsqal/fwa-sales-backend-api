"""MSISDN procurement and hard-bundle pairing.

Two use cases, one aggregate. A Device Partner asks IOH for numbers; IOH supplies them
straight into ``fwa_inventory``; the Device Partner later pairs an IMEI to each. They
share a PO, a status machine, and an org-scoping rule, which is why they share a module.

The counting rules here are not validation politeness. The business process is explicit
that the number of IMEIs must equal the number of MSISDNs, and every batch write is
all-or-nothing: a half-applied import of 400 numbers is worse than a failed one,
because nothing about the result tells you which half landed.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError, ValidationFailedError
from app.core.msisdn import normalise_msisdn
from app.core.pagination import Pagination
from app.models.admin import AdminUser
from app.models.commercial import Brand, CallPlan
from app.models.enums import AdminRole, InventoryStatus, MsisdnPoStatus
from app.models.inventory import FwaInventory
from app.models.purchasing import MsisdnPo, MsisdnPoStatusHistory
from app.schemas.purchasing import (
    CreateMsisdnPoRequest,
    MsisdnOut,
    MsisdnPoDetailOut,
    MsisdnPoOut,
    PairingResultOut,
    PairingRow,
    PairingValidationOut,
    StatusHistoryOut,
    SupplyResultOut,
    SupplyValidationOut,
)
from app.schemas.reference import BrandOut, CallPlanOut, DevicePartnerRefOut
from app.services.supply_common import (
    ensure_transition,
    next_msisdn_po_code,
    require_exact_count,
    require_no_duplicates,
)

IMEI_PATTERN: Final = re.compile(r"^[0-9]{14,16}$")


# ---------------------------------------------------------------------------
# Scoping — golden rule 7
# ---------------------------------------------------------------------------


def _scope(statement: Select[Any], admin: AdminUser) -> Select[Any]:
    """Narrow a query to what this principal may see.

    The binding comes from the token and nothing widens it. IOH is global-read by
    design; an MPX has no business in MSISDN procurement at all.
    """
    if admin.role is AdminRole.DP_ADMIN:
        return statement.where(MsisdnPo.device_partner_id == admin.device_partner_id)
    if admin.role is AdminRole.IOH_ADMIN:
        return statement
    raise ForbiddenError(
        "Your role does not have access to this action.", code="ROLE_NOT_PERMITTED"
    )


async def _get_scoped(session: AsyncSession, admin: AdminUser, po_id: uuid.UUID) -> MsisdnPo:
    """404, never 403, for a PO belonging to another Device Partner.

    Same reasoning as the AE rule: a 403 would confirm the order exists, and these are
    three competing companies reading one dataset.
    """
    po: MsisdnPo | None = await session.scalar(
        _scope(select(MsisdnPo), admin).where(MsisdnPo.msisdn_po_id == po_id)
    )
    if po is None:
        raise NotFoundError("This purchase order does not exist.", code="PO_NOT_FOUND")
    return po


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


async def _qty_supplied(session: AsyncSession, po_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(FwaInventory).where(FwaInventory.msisdn_po_id == po_id)
        )
        or 0
    )


def _to_out(po: MsisdnPo, *, qty_supplied: int) -> MsisdnPoOut:
    return MsisdnPoOut(
        msisdn_po_id=po.msisdn_po_id,
        po_code=po.po_code,
        device_partner=DevicePartnerRefOut(
            device_partner_id=po.device_partner.device_partner_id,
            code=po.device_partner.code,
            name=po.device_partner.name,
        ),
        call_plan=CallPlanOut(
            call_plan_id=po.call_plan.call_plan_id,
            code=po.call_plan.code,
            name=po.call_plan.name,
            kind=po.call_plan.kind,
            quota_gb=po.call_plan.quota_gb,
            sort_order=po.call_plan.sort_order,
        ),
        brand=BrandOut(
            code=po.brand.code,
            display_name=po.brand.display_name,
            outlet_name=po.brand.outlet_name,
            sort_order=po.brand.sort_order,
        ),
        qty_requested=po.qty_requested,
        qty_supplied=qty_supplied,
        status=po.status,
        note=po.note,
        rejected_reason=po.rejected_reason,
        submitted_at=po.submitted_at,
        processed_at=po.processed_at,
        completed_at=po.completed_at,
    )


def _history_out(rows: list[MsisdnPoStatusHistory]) -> list[StatusHistoryOut]:
    return [
        StatusHistoryOut(
            changed_at=row.changed_at,
            old_status=row.old_status,
            new_status=row.new_status,
            changed_by=row.changed_by.full_name if row.changed_by is not None else None,
            note=row.note,
        )
        for row in rows
    ]


def _record(
    po: MsisdnPo, *, old: str | None, new: MsisdnPoStatus, admin: AdminUser, note: str | None
) -> MsisdnPoStatusHistory:
    """Build the history row. Added to the session inside the caller's transaction, so
    the status change and its record commit together or not at all."""
    return MsisdnPoStatusHistory(
        msisdn_po_id=po.msisdn_po_id,
        old_status=old,
        new_status=new.value,
        changed_by_admin_id=admin.admin_user_id,
        note=note,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def list_pos(
    session: AsyncSession,
    admin: AdminUser,
    *,
    status: MsisdnPoStatus | None,
    q: str | None,
    pagination: Pagination,
) -> tuple[list[MsisdnPoOut], int]:
    filters = []
    if status is not None:
        filters.append(MsisdnPo.status == status)
    if q:
        filters.append(MsisdnPo.po_code.ilike(f"%{q.strip()}%"))

    total = int(
        await session.scalar(
            _scope(select(func.count()).select_from(MsisdnPo), admin).where(*filters)
        )
        or 0
    )
    rows = (
        await session.scalars(
            _scope(select(MsisdnPo), admin)
            .where(*filters)
            .order_by(MsisdnPo.submitted_at.desc())
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
    ).all()

    counted = (
        await session.execute(
            select(FwaInventory.msisdn_po_id, func.count())
            .where(FwaInventory.msisdn_po_id.in_([row.msisdn_po_id for row in rows]))
            .group_by(FwaInventory.msisdn_po_id)
        )
    ).all()
    supplied: dict[uuid.UUID, int] = {po_id: count for po_id, count in counted if po_id is not None}
    return [_to_out(row, qty_supplied=supplied.get(row.msisdn_po_id, 0)) for row in rows], total


async def get_po(session: AsyncSession, admin: AdminUser, po_id: uuid.UUID) -> MsisdnPoDetailOut:
    po = await _get_scoped(session, admin, po_id)
    history = (
        await session.scalars(
            select(MsisdnPoStatusHistory)
            .where(MsisdnPoStatusHistory.msisdn_po_id == po_id)
            # history_id, not just changed_at: now() is transaction start time in
            # PostgreSQL, so rows written in one transaction share a timestamp and
            # the sort would be unstable. The Riwayat panel is read by three
            # companies; it cannot render its own history out of order.
            .order_by(MsisdnPoStatusHistory.changed_at, MsisdnPoStatusHistory.history_id)
        )
    ).all()
    base = _to_out(po, qty_supplied=await _qty_supplied(session, po_id))
    return MsisdnPoDetailOut(**base.model_dump(), status_history=_history_out(list(history)))


async def list_msisdns(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID
) -> list[MsisdnOut]:
    """Backs the download icon beside a supplied PO."""
    await _get_scoped(session, admin, po_id)
    rows = (
        await session.scalars(
            select(FwaInventory)
            .where(FwaInventory.msisdn_po_id == po_id)
            .order_by(FwaInventory.msisdn)
        )
    ).all()
    return [
        MsisdnOut(
            msisdn=row.msisdn, imei=row.imei, status=row.status.value, paired_at=row.paired_at
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# DP: create and cancel
# ---------------------------------------------------------------------------


async def create_po(
    session: AsyncSession, admin: AdminUser, payload: CreateMsisdnPoRequest
) -> MsisdnPoOut:
    if admin.role is not AdminRole.DP_ADMIN or admin.device_partner is None:
        raise ForbiddenError(
            "Only a Device Partner can raise an MSISDN request.", code="ROLE_NOT_PERMITTED"
        )

    call_plan = await session.get(CallPlan, payload.call_plan_id)
    if call_plan is None or not call_plan.is_active:
        raise ValidationFailedError(
            "That call plan is not available.",
            code="CALL_PLAN_NOT_FOUND",
            details=[{"field": "callPlanId", "issue": "unknown or inactive"}],
        )
    brand = await session.get(Brand, payload.brand_code)
    if brand is None or not brand.is_active:
        raise ValidationFailedError(
            "That brand is not available.",
            code="BRAND_NOT_FOUND",
            details=[{"field": "brandCode", "issue": "unknown or inactive"}],
        )

    po = MsisdnPo(
        po_code=await next_msisdn_po_code(
            session, dp_code=admin.device_partner.code, on=datetime.now(UTC).date()
        ),
        device_partner_id=admin.device_partner_id,
        call_plan_id=call_plan.call_plan_id,
        brand_code=brand.code,
        qty_requested=payload.qty_requested,
        status=MsisdnPoStatus.DIAJUKAN,
        note=payload.note,
        created_by_admin_id=admin.admin_user_id,
    )
    session.add(po)
    await session.flush()
    session.add(_record(po, old=None, new=MsisdnPoStatus.DIAJUKAN, admin=admin, note=payload.note))
    await session.refresh(po)
    return _to_out(po, qty_supplied=0)


async def cancel_po(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, note: str | None
) -> MsisdnPoOut:
    """A DP may withdraw only before IOH has acted on it."""
    if admin.role is not AdminRole.DP_ADMIN:
        raise ForbiddenError(
            "Only the requesting Device Partner can cancel.", code="ROLE_NOT_PERMITTED"
        )
    po = await _get_scoped(session, admin, po_id)
    ensure_transition(
        current=po.status.value,
        target=MsisdnPoStatus.DIBATALKAN.value,
        allowed_from={MsisdnPoStatus.DIAJUKAN.value},
    )
    old = po.status.value
    po.status = MsisdnPoStatus.DIBATALKAN
    session.add(_record(po, old=old, new=po.status, admin=admin, note=note))
    return _to_out(po, qty_supplied=0)


# ---------------------------------------------------------------------------
# IOH: process, reject, supply
# ---------------------------------------------------------------------------


def _require_ioh(admin: AdminUser) -> None:
    if admin.role is not AdminRole.IOH_ADMIN:
        raise ForbiddenError("Only IOH can act on an MSISDN request.", code="ROLE_NOT_PERMITTED")


async def process_po(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, note: str | None
) -> MsisdnPoOut:
    _require_ioh(admin)
    po = await _get_scoped(session, admin, po_id)
    ensure_transition(
        current=po.status.value,
        target=MsisdnPoStatus.DIPROSES.value,
        allowed_from={MsisdnPoStatus.DIAJUKAN.value},
    )
    old = po.status.value
    po.status = MsisdnPoStatus.DIPROSES
    po.processed_at = datetime.now(UTC)
    session.add(_record(po, old=old, new=po.status, admin=admin, note=note))
    return _to_out(po, qty_supplied=await _qty_supplied(session, po_id))


async def reject_po(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, reason: str
) -> MsisdnPoOut:
    """Confirmed 2026-09-01: IOH genuinely has discretion to refuse.

    The reason is mandatory in the schema and again in the database — a refusal a
    Device Partner cannot act on leaves the request stuck at DIAJUKAN forever, which is
    the dead end this state exists to prevent.
    """
    _require_ioh(admin)
    po = await _get_scoped(session, admin, po_id)
    ensure_transition(
        current=po.status.value,
        target=MsisdnPoStatus.DITOLAK.value,
        allowed_from={MsisdnPoStatus.DIAJUKAN.value, MsisdnPoStatus.DIPROSES.value},
    )
    old = po.status.value
    po.status = MsisdnPoStatus.DITOLAK
    po.rejected_reason = reason
    po.completed_at = datetime.now(UTC)
    session.add(_record(po, old=old, new=po.status, admin=admin, note=reason))
    return _to_out(po, qty_supplied=await _qty_supplied(session, po_id))


async def validate_supply(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, msisdns: list[str]
) -> SupplyValidationOut:
    """Dry run for the XLS import. Writes nothing.

    Golden rule 11 in practice: a spreadsheet column of ``08…`` numbers is normalised
    here, before validation, or the ``^62…`` CHECK would reject a perfectly good upload
    and the operator would have no idea why.
    """
    _require_ioh(admin)
    po = await _get_scoped(session, admin, po_id)

    errors: list[dict[str, str]] = []
    accepted: list[str] = []
    seen: set[str] = set()

    for raw in msisdns:
        normalised = normalise_msisdn(raw)
        if normalised is None:
            errors.append({"value": raw, "issue": "not a valid Indonesian MSISDN"})
            continue
        if normalised in seen:
            errors.append({"value": raw, "issue": "duplicated in this batch"})
            continue
        seen.add(normalised)
        accepted.append(normalised)

    if accepted:
        taken = set(
            (
                await session.scalars(
                    select(FwaInventory.msisdn).where(FwaInventory.msisdn.in_(accepted))
                )
            ).all()
        )
        if taken:
            accepted = [value for value in accepted if value not in taken]
            errors.extend({"value": value, "issue": "already issued"} for value in sorted(taken))

    if len(accepted) != po.qty_requested and not errors:
        errors.append(
            {
                "value": str(len(accepted)),
                "issue": f"expected exactly {po.qty_requested} numbers",
            }
        )

    return SupplyValidationOut(
        ok=not errors and len(accepted) == po.qty_requested,
        expected=po.qty_requested,
        received=len(msisdns),
        accepted=accepted,
        errors=errors,
    )


async def supply(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, msisdns: list[str]
) -> SupplyResultOut:
    """Issue the numbers. One transaction, all or nothing.

    The supplied MSISDNs become ``fwa_inventory`` rows directly — there is no item
    table, because a supplied number *is* a unit. They land at ``MSISDN_ISSUED`` with no
    IMEI, which is the one state the AE app cannot see.
    """
    _require_ioh(admin)
    po = await _get_scoped(session, admin, po_id)
    ensure_transition(
        current=po.status.value,
        target=MsisdnPoStatus.DITERIMA.value,
        # Supplying implicitly picks the request up, so DIAJUKAN is accepted as well as
        # DIPROSES — the IOH dashboard offers one action, not two.
        allowed_from={MsisdnPoStatus.DIAJUKAN.value, MsisdnPoStatus.DIPROSES.value},
    )

    normalised: list[str] = []
    for raw in msisdns:
        value = normalise_msisdn(raw)
        if value is None:
            raise ValidationFailedError(
                f"{raw} is not a valid Indonesian MSISDN. Nothing was saved.",
                code="INVALID_MSISDN",
                details=[{"field": "msisdns", "issue": f"invalid: {raw}"}],
            )
        normalised.append(value)

    require_no_duplicates(normalised, field="msisdns", code="DUPLICATE_MSISDN")
    require_exact_count(len(normalised), po.qty_requested, field="msisdns", code="QTY_MISMATCH")

    taken = (
        await session.scalars(
            select(FwaInventory.msisdn).where(FwaInventory.msisdn.in_(normalised))
        )
    ).all()
    if taken:
        raise ValidationFailedError(
            "Some of these numbers have already been issued. Nothing was saved.",
            code="MSISDN_ALREADY_ISSUED",
            details=[{"field": "msisdns", "issue": f"already issued: {sorted(taken)[:5]}"}],
        )

    for value in normalised:
        session.add(
            FwaInventory(
                msisdn=value,
                imei=None,
                status=InventoryStatus.MSISDN_ISSUED,
                msisdn_po_id=po.msisdn_po_id,
                brand_code=po.brand_code,
                call_plan_id=po.call_plan_id,
            )
        )

    old = po.status.value
    po.status = MsisdnPoStatus.DITERIMA
    po.supplied_by_admin_id = admin.admin_user_id
    po.completed_at = datetime.now(UTC)
    session.add(
        _record(
            po,
            old=old,
            new=po.status,
            admin=admin,
            note=f"{len(normalised)} nomor disediakan",
        )
    )
    await session.flush()
    return SupplyResultOut(msisdn_po_id=po.msisdn_po_id, status=po.status, supplied=len(normalised))


# ---------------------------------------------------------------------------
# DP: hard-bundle pairing
# ---------------------------------------------------------------------------


async def _pairable(session: AsyncSession, po_id: uuid.UUID) -> dict[str, FwaInventory]:
    rows = (
        await session.scalars(
            select(FwaInventory).where(
                FwaInventory.msisdn_po_id == po_id,
                FwaInventory.status == InventoryStatus.MSISDN_ISSUED,
            )
        )
    ).all()
    return {row.msisdn: row for row in rows}


def _require_dp(admin: AdminUser) -> None:
    if admin.role is not AdminRole.DP_ADMIN:
        raise ForbiddenError("Only a Device Partner can pair bundles.", code="ROLE_NOT_PERMITTED")


async def _check_pairs(
    session: AsyncSession, po_id: uuid.UUID, pairs: list[PairingRow]
) -> tuple[list[PairingRow], list[dict[str, str]]]:
    """Shared by the dry run and the real thing, so the preview cannot drift from it."""
    pairable = await _pairable(session, po_id)
    errors: list[dict[str, str]] = []
    accepted: list[PairingRow] = []
    seen_msisdn: set[str] = set()
    seen_imei: set[str] = set()

    for row in pairs:
        msisdn = normalise_msisdn(row.msisdn)
        imei = row.imei.strip()

        if msisdn is None:
            errors.append({"value": row.msisdn, "issue": "not a valid Indonesian MSISDN"})
            continue
        if not IMEI_PATTERN.match(imei):
            errors.append({"value": row.imei, "issue": "an IMEI is 14 to 16 digits"})
            continue
        if msisdn not in pairable:
            errors.append({"value": msisdn, "issue": "not an unpaired number on this PO"})
            continue
        if msisdn in seen_msisdn:
            errors.append({"value": msisdn, "issue": "duplicated in this batch"})
            continue
        if imei in seen_imei:
            errors.append({"value": imei, "issue": "duplicated in this batch"})
            continue
        seen_msisdn.add(msisdn)
        seen_imei.add(imei)
        accepted.append(PairingRow(msisdn=msisdn, imei=imei))

    if accepted:
        clashes = {
            imei
            for imei in (
                await session.scalars(
                    select(FwaInventory.imei).where(
                        FwaInventory.imei.in_([row.imei for row in accepted])
                    )
                )
            ).all()
            # An IN filter cannot match NULL, but imei is nullable on the model, so the
            # narrowing is stated rather than assumed.
            if imei is not None
        }
        if clashes:
            accepted = [row for row in accepted if row.imei not in clashes]
            errors.extend(
                {"value": imei, "issue": "already paired to another number"}
                for imei in sorted(clashes)
            )

    return accepted, errors


async def validate_pairing(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, pairs: list[PairingRow]
) -> PairingValidationOut:
    _require_dp(admin)
    await _get_scoped(session, admin, po_id)
    expected = len(await _pairable(session, po_id))
    accepted, errors = await _check_pairs(session, po_id, pairs)
    return PairingValidationOut(
        ok=not errors and len(accepted) == expected,
        expected=expected,
        received=len(pairs),
        accepted=accepted,
        errors=errors,
    )


async def pair(
    session: AsyncSession, admin: AdminUser, po_id: uuid.UUID, *, pairs: list[PairingRow]
) -> PairingResultOut:
    """Attach an IMEI to every number on this PO.

    The count must match exactly. A partial pairing is rejected rather than applied:
    the business process requires one IMEI per MSISDN, and half a bundle is a unit
    nobody can ship and nobody can find.
    """
    _require_dp(admin)
    po = await _get_scoped(session, admin, po_id)
    if po.status is not MsisdnPoStatus.DITERIMA:
        raise ValidationFailedError(
            "Numbers must be supplied before they can be paired.",
            code="PO_NOT_SUPPLIED",
            details=[{"field": "status", "issue": f"expected DITERIMA, got {po.status.value}"}],
        )

    pairable = await _pairable(session, po_id)
    accepted, errors = await _check_pairs(session, po_id, pairs)
    if errors:
        raise ValidationFailedError(
            "Some rows could not be paired. Nothing was saved.",
            code="PAIRING_REJECTED",
            details=[{"field": row["value"], "issue": row["issue"]} for row in errors[:10]],
        )
    require_exact_count(len(accepted), len(pairable), field="pairs", code="IMEI_COUNT_MISMATCH")

    now = datetime.now(UTC)
    for row in accepted:
        unit = pairable[row.msisdn]
        unit.imei = row.imei
        unit.status = InventoryStatus.PAIRED
        unit.paired_at = now
        unit.paired_by_admin_id = admin.admin_user_id

    await session.flush()
    return PairingResultOut(msisdn_po_id=po.msisdn_po_id, paired=len(accepted))
