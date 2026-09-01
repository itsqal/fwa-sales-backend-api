"""Machinery shared by every write in the supply module.

Three concerns live here because all three purchase-order flows need them and none of
them belongs to a single use case: server-generated PO codes, replay safety for bulk
writes, and the transition guard behind golden rule 8.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ValidationFailedError
from app.models.purchasing import IdempotencyRecord

# ---------------------------------------------------------------------------
# PO codes
# ---------------------------------------------------------------------------


async def next_msisdn_po_code(session: AsyncSession, *, dp_code: str, on: date) -> str:
    """``{DP_CODE}-{YYYYMMDD}-{SEQ}`` — e.g. ``ADVAN-20250523-207``.

    The sequence is what makes this safe: two Device Partners submitting at the same
    instant cannot be handed the same number, and ``nextval`` is not rolled back by a
    failed transaction, so a gap is possible but a collision is not. Gaps in a PO
    number are harmless; duplicates are not.
    """
    seq = await session.scalar(text("SELECT nextval('msisdn_po_seq')"))
    return f"{dp_code}-{on:%Y%m%d}-{seq}"


async def next_device_po_code(
    session: AsyncSession,
    *,
    circle: str | None,
    brand_code: str,
    model_code: str,
    on: date,
) -> str:
    """``PO-{CIRCLE}-{BRAND}-{MODEL}-{YYYYMMDD}-{SEQ}``.

    Read off the Detail PO modal: ``PO-JAVA-3ID-RABIT CPE-R-28-20250809-407``. The model
    code contains spaces and dashes of its own, which is why the column is generous and
    why nothing ever parses this string back apart — the components are all stored as
    their own columns.
    """
    seq = await session.scalar(text("SELECT nextval('device_po_seq')"))
    return f"PO-{circle or 'NA'}-{brand_code}-{model_code}-{on:%Y%m%d}-{seq}"


# ---------------------------------------------------------------------------
# Replay safety for bulk writes
# ---------------------------------------------------------------------------


def hash_request(payload: Any) -> str:
    """Stable digest of a request body, for detecting a key reused with new content."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def replay(
    session: AsyncSession,
    *,
    admin_user_id: uuid.UUID,
    endpoint: str,
    key: uuid.UUID | None,
    request_hash: str,
) -> dict[str, Any] | None:
    """Return the original response for this key, or ``None`` if it is new.

    A key seen before with a *different* payload is a client bug and raises 409 rather
    than answering the wrong question — the same rule the AE endpoints follow.
    """
    if key is None:
        return None

    record = await session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.admin_user_id == admin_user_id,
            IdempotencyRecord.endpoint == endpoint,
            IdempotencyRecord.key == key,
        )
    )
    if record is None:
        return None

    if record.request_hash != request_hash:
        raise ConflictError(
            "This request was already submitted with different content.",
            code="IDEMPOTENCY_KEY_REUSED",
        )
    return dict(record.response_body)


async def remember(
    session: AsyncSession,
    *,
    admin_user_id: uuid.UUID,
    endpoint: str,
    key: uuid.UUID | None,
    request_hash: str,
    response_body: dict[str, Any],
    status_code: int = 200,
) -> None:
    """Store the response so a retry returns it instead of repeating the work."""
    if key is None:
        return
    session.add(
        IdempotencyRecord(
            admin_user_id=admin_user_id,
            endpoint=endpoint,
            key=key,
            request_hash=request_hash,
            response_body=response_body,
            status_code=status_code,
        )
    )
    await session.flush()


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def ensure_transition(*, current: str, target: str, allowed_from: set[str]) -> None:
    """Guard a named transition endpoint.

    Golden rule 8: no endpoint accepts a status in its body. Every change is a named
    action that validates the state it is coming from, so a PO cannot skip a step or go
    backwards, and the history is a true record rather than a suggestion.

    409 rather than 422 because nothing about the request is malformed — the order is
    simply not in a state where this action means anything, and that is usually a stale
    browser tab rather than a bad client.
    """
    if current not in allowed_from:
        raise ConflictError(
            f"This order is {current} and cannot be moved to {target}.",
            code="INVALID_TRANSITION",
            details=[{"field": "status", "issue": f"expected one of {sorted(allowed_from)}"}],
        )


# ---------------------------------------------------------------------------
# Bulk input hygiene
# ---------------------------------------------------------------------------


def require_no_duplicates(values: list[str], *, field: str, code: str) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    if duplicates:
        raise ValidationFailedError(
            f"The same {field} appears more than once in this batch.",
            code=code,
            details=[{"field": field, "issue": f"duplicated: {', '.join(duplicates[:5])}"}],
        )


def require_exact_count(actual: int, expected: int, *, field: str, code: str) -> None:
    """Golden rule 9. The business process is explicit: the counts must match.

    Rejecting the whole batch is deliberate — a partially applied import is worse than
    a failed one, because nobody can tell by looking which half landed.
    """
    if actual != expected:
        raise ValidationFailedError(
            f"Expected {expected} {field}, received {actual}. Nothing was saved.",
            code=code,
            details=[{"field": field, "issue": f"expected {expected}, got {actual}"}],
        )
