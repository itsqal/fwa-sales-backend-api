"""MSISDN normalisation — the rule that keeps a scanned barcode findable."""

from __future__ import annotations

import pytest

from app.core.msisdn import is_valid_phone_number, mask_msisdn, normalise_msisdn


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("6285882724305", "6285882724305"),
        ("085882724305", "6285882724305"),
        ("+6285882724305", "6285882724305"),
        ("62 858 8272 4305", "6285882724305"),
        ("0858-8272-4305", "6285882724305"),
        ("  6285882724305  ", "6285882724305"),
    ],
)
def test_normalise_accepts_the_shapes_a_scanner_produces(raw: str, expected: str) -> None:
    assert normalise_msisdn(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "abc",
        "62",
        "621234",  # too short for the CHECK constraint
        "6212345678901234567",  # too long
        "1285882724305",  # not an Indonesian prefix
        "62858827243o5",  # letter that looks like a digit
    ],
)
def test_normalise_rejects_everything_else(raw: str) -> None:
    assert normalise_msisdn(raw) is None


def test_normalisation_is_idempotent() -> None:
    once = normalise_msisdn("085882724305")
    assert once is not None
    assert normalise_msisdn(once) == once


@pytest.mark.parametrize("raw", ["082234567890", "6282234567890"])
def test_customer_phone_accepts_both_forms(raw: str) -> None:
    """A customer's contact number is not an FWA MSISDN and is stored as entered."""
    assert is_valid_phone_number(raw) is True


@pytest.mark.parametrize("raw", ["1234", "not-a-phone", "+6282234567890"])
def test_customer_phone_rejects_the_rest(raw: str) -> None:
    assert is_valid_phone_number(raw) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("6285882724305", "****4305"),
        ("355806671396654", "****6654"),
        ("123", "****"),
        (None, "****"),
    ],
)
def test_masking_never_leaks_more_than_the_last_four(raw, expected) -> None:
    """Never log credentials, tokens, full MSISDNs or IMEIs."""
    assert mask_msisdn(raw) == expected
