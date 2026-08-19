"""Device-clock auditing.

``deviceTime`` is accepted on every field write and used for **nothing**. Field phones
have wrong clocks, sometimes deliberately, so the server timestamps its own rows.

The value is not discarded either: the skew between the device clock and the server is
logged, which is what lets you reconstruct an AE's day order later and spot a handset
whose clock has been moved. There is no column for it on ``customer``, ``activation`` or
``attendance`` — this log line is the whole audit trail, which is worth knowing before
someone asks for a device-time report.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

logger = logging.getLogger("app.audit.device_time")

# Below this, a phone is simply not perfectly synchronised and nobody cares.
_INTERESTING_SKEW_SECONDS = 120


def record_device_time(*, ae_id: uuid.UUID, action: str, device_time: datetime | None) -> None:
    if device_time is None:
        return

    if device_time.tzinfo is None:
        device_time = device_time.replace(tzinfo=UTC)

    skew = (datetime.now(UTC) - device_time).total_seconds()
    if abs(skew) < _INTERESTING_SKEW_SECONDS:
        return

    logger.info(
        "Device clock skew on %s: %.0fs (ae=%s). Server time was used for the record.",
        action,
        skew,
        ae_id,
    )
