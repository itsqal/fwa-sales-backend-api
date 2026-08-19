"""MSISDN normalisation. Applied once, at the request edge, and never deeper.

A barcode may scan as ``085882724305``; it is stored and compared as
``6285882724305``. The database enforces ``^62[0-9]{8,13}$`` with a CHECK constraint,
so anything that slips past this function fails loudly rather than quietly creating a
row that no lookup will ever match.
"""

from __future__ import annotations

import re
from typing import Final

MSISDN_PATTERN: Final = re.compile(r"^62[0-9]{8,13}$")

# Customer contact numbers are a different thing from an FWA MSISDN: they are stored
# as the AE typed them, in either 08xx or 62xx form, per the contract's examples.
PHONE_PATTERN: Final = re.compile(r"^(62|0)[0-9]{8,13}$")


def normalise_msisdn(raw: str) -> str | None:
    """Return the ``62`` form of ``raw``, or ``None`` if it cannot be one.

    Accepts the shapes a scanner or a human realistically produces — spaces, dashes,
    a leading ``+``, a leading ``0`` — and rejects everything else.
    """
    if not raw:
        return None

    cleaned = re.sub(r"[\s\-().]", "", raw.strip())
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]

    if not cleaned.isdigit():
        return None

    if cleaned.startswith("0"):
        cleaned = "62" + cleaned[1:]

    return cleaned if MSISDN_PATTERN.match(cleaned) else None


def is_valid_phone_number(raw: str) -> bool:
    """True if ``raw`` satisfies the customer phone CHECK constraint."""
    return bool(PHONE_PATTERN.match(raw))


def mask_msisdn(value: str | None) -> str:
    """Mask an MSISDN or IMEI for logging. Never log the full number."""
    if not value:
        return "****"
    return f"****{value[-4:]}" if len(value) > 4 else "****"
