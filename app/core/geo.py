"""GPS fix evaluation.

Two separate questions, deliberately kept apart:

* ``verified`` — did the fix meet the accuracy tolerance and come from a real provider?
* ``is_mocked`` — did Android report a mock-location provider?

A mocked fix is recorded and flagged for supervisor review, never used to reject a
submission. Fake GPS is a genuine fraud vector, but a real AE hitting a device quirk
must not be blocked in the field.
"""

from __future__ import annotations

from app.core.config import Settings


def is_verified(settings: Settings, *, accuracy_m: float | None, is_mocked: bool) -> bool:
    if is_mocked:
        return False
    # An absent accuracy reading cannot be checked against a tolerance, so it is not
    # treated as passing. The submission is still accepted.
    if accuracy_m is None:
        return False
    return accuracy_m <= settings.geo_accuracy_tolerance_m
