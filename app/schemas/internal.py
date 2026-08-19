"""Server-to-server payloads. Not reachable by the mobile app."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator

from app.core.msisdn import normalise_msisdn
from app.schemas.common import CamelModel


class GaOutcome(StrEnum):
    ACTIVATED = "ACTIVATED"
    FAILED = "FAILED"


class GaEvent(CamelModel):
    msisdn: str
    activation_date: datetime
    outcome: GaOutcome = GaOutcome.ACTIVATED

    @field_validator("msisdn")
    @classmethod
    def _normalise(cls, value: str) -> str:
        # An unparseable number is not an error here: the feed is a firehose from GCP
        # and one bad row must not fail the batch. It is passed through unchanged and
        # counted as unmatched.
        return normalise_msisdn(value) or value


class GaEventBatch(CamelModel):
    events: list[GaEvent] = Field(min_length=1)


class GaIngestResult(CamelModel):
    matched: int
    unmatched: int
