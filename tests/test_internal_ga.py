"""Gross Add ingestion — the only path allowed to write activation_date."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.activation import Activation
from app.models.enums import ActivationStatus, IncentiveEventType, InventoryStatus, LedgerStatus
from app.models.incentive import IncentiveLedger, IncentiveRule


async def _activate(client, headers, customer_id: int, msisdn: str) -> int:
    response = await client.post(
        "/activations",
        json={
            "customerId": customer_id,
            "msisdn": msisdn,
            "latitude": -6.2,
            "longitude": 106.9,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["activationId"]


@pytest.fixture
async def submitted(client, auth_headers, ae, make_inventory, make_customer):
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)
    activation_id = await _activate(client, auth_headers, customer.customer_id, item.msisdn)
    return item, customer, activation_id


async def test_ga_event_activates_and_stamps_the_date(
    client: AsyncClient, service_headers, submitted, session
) -> None:
    item, _, activation_id = submitted

    response = await client.post(
        "/internal/ga-events",
        json={"events": [{"msisdn": item.msisdn, "activationDate": "2026-08-11T09:00:00+07:00"}]},
        headers=service_headers,
    )

    assert response.status_code == 202
    assert response.json() == {"matched": 1, "unmatched": 0}

    stored = await session.get(Activation, activation_id)
    await session.refresh(stored)
    assert stored.status is ActivationStatus.ACTIVATED
    assert stored.activation_date is not None
    assert stored.ga_synced_at is not None


async def test_ga_event_flips_the_badge_in_the_ae_facing_list(
    client: AsyncClient, auth_headers, service_headers, submitted
) -> None:
    item, _, _ = submitted

    before = await client.get("/activations", headers=auth_headers)
    await client.post(
        "/internal/ga-events",
        json={"events": [{"msisdn": item.msisdn, "activationDate": "2026-08-11T09:00:00+07:00"}]},
        headers=service_headers,
    )
    after = await client.get("/activations", headers=auth_headers)

    assert before.json()["data"][0]["status"] == "NOT_ACTIVATED"
    assert after.json()["data"][0]["status"] == "ACTIVATED"
    assert after.json()["data"][0]["activationDate"] is not None


async def test_ga_event_marks_the_unit_activated(
    client: AsyncClient, service_headers, submitted, session
) -> None:
    item, _, _ = submitted

    await client.post(
        "/internal/ga-events",
        json={"events": [{"msisdn": item.msisdn, "activationDate": "2026-08-11T09:00:00+07:00"}]},
        headers=service_headers,
    )

    await session.refresh(item)
    assert item.status is InventoryStatus.ACTIVATED


async def test_unknown_msisdn_is_counted_unmatched_without_crashing(
    client: AsyncClient, service_headers
) -> None:
    """A feed is a firehose. An MSISDN nobody here sold is normal, not an error."""
    response = await client.post(
        "/internal/ga-events",
        json={
            "events": [
                {"msisdn": "6289999999999", "activationDate": "2026-08-11T09:00:00+07:00"},
                {"msisdn": "garbage", "activationDate": "2026-08-11T09:00:00+07:00"},
            ]
        },
        headers=service_headers,
    )

    assert response.status_code == 202
    assert response.json() == {"matched": 0, "unmatched": 2}


async def test_a_bad_row_does_not_stop_the_batch(
    client: AsyncClient, service_headers, submitted
) -> None:
    item, _, _ = submitted

    response = await client.post(
        "/internal/ga-events",
        json={
            "events": [
                {"msisdn": "6289999999999", "activationDate": "2026-08-11T09:00:00+07:00"},
                {"msisdn": item.msisdn, "activationDate": "2026-08-11T09:00:00+07:00"},
            ]
        },
        headers=service_headers,
    )

    assert response.json() == {"matched": 1, "unmatched": 1}


async def test_failed_outcome_does_not_stamp_a_date(
    client: AsyncClient, service_headers, submitted, session
) -> None:
    item, _, activation_id = submitted

    await client.post(
        "/internal/ga-events",
        json={
            "events": [
                {
                    "msisdn": item.msisdn,
                    "activationDate": "2026-08-11T09:00:00+07:00",
                    "outcome": "FAILED",
                }
            ]
        },
        headers=service_headers,
    )

    stored = await session.get(Activation, activation_id)
    await session.refresh(stored)
    assert stored.status is ActivationStatus.FAILED
    assert stored.activation_date is None


async def test_ga_endpoint_rejects_an_ae_token(
    client: AsyncClient, auth_headers, submitted
) -> None:
    """This is a service-credential path. An AE token must not reach it."""
    item, _, _ = submitted

    response = await client.post(
        "/internal/ga-events",
        json={"events": [{"msisdn": item.msisdn, "activationDate": "2026-08-11T09:00:00+07:00"}]},
        headers=auth_headers,
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "INVALID_SERVICE_CREDENTIAL"


async def test_ga_endpoint_rejects_a_wrong_service_key(client: AsyncClient, submitted) -> None:
    item, _, _ = submitted

    response = await client.post(
        "/internal/ga-events",
        json={"events": [{"msisdn": item.msisdn, "activationDate": "2026-08-11T09:00:00+07:00"}]},
        headers={"X-Service-Key": "wrong"},
    )

    assert response.status_code == 403


async def test_ga_payload_validation(client: AsyncClient, service_headers) -> None:
    response = await client.post(
        "/internal/ga-events", json={"events": []}, headers=service_headers
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Incentive accrual. The engine is data-driven; these tests supply their own rule
# rather than relying on a seeded amount, because the real amounts are undecided.
# ---------------------------------------------------------------------------


@pytest.fixture
async def activation_rule(session, today) -> IncentiveRule:
    rule = IncentiveRule(
        rule_name="Test activation bonus",
        event_type=IncentiveEventType.ACTIVATION,
        amount_idr=Decimal("50000.00"),
        effective_from=today - timedelta(days=365),
        effective_to=None,
    )
    session.add(rule)
    await session.flush()
    return rule


async def test_confirmed_ga_accrues_against_a_configured_rule(
    client: AsyncClient, service_headers, submitted, activation_rule, session, today
) -> None:
    item, _, activation_id = submitted

    await client.post(
        "/internal/ga-events",
        json={"events": [{"msisdn": item.msisdn, "activationDate": datetime.now(UTC).isoformat()}]},
        headers=service_headers,
    )

    entries = (
        await session.scalars(
            select(IncentiveLedger).where(IncentiveLedger.activation_id == activation_id)
        )
    ).all()
    assert len(entries) == 1
    assert entries[0].amount_idr == Decimal("50000.00")
    assert entries[0].period_ym == today.strftime("%Y-%m")
    assert entries[0].status is LedgerStatus.ACCRUED


async def test_no_rules_means_no_accrual(
    client: AsyncClient, service_headers, submitted, session
) -> None:
    """With the amounts undecided there are no rules, so nothing accrues. Zero is the
    honest answer, not a plausible constant."""
    item, _, activation_id = submitted

    await client.post(
        "/internal/ga-events",
        json={"events": [{"msisdn": item.msisdn, "activationDate": datetime.now(UTC).isoformat()}]},
        headers=service_headers,
    )

    entries = (
        await session.scalars(
            select(IncentiveLedger).where(IncentiveLedger.activation_id == activation_id)
        )
    ).all()
    assert entries == []


async def test_a_replayed_ga_event_does_not_pay_twice(
    client: AsyncClient, service_headers, submitted, activation_rule, session
) -> None:
    item, _, activation_id = submitted
    event = {"events": [{"msisdn": item.msisdn, "activationDate": datetime.now(UTC).isoformat()}]}

    await client.post("/internal/ga-events", json=event, headers=service_headers)
    await client.post("/internal/ga-events", json=event, headers=service_headers)

    entries = (
        await session.scalars(
            select(IncentiveLedger).where(IncentiveLedger.activation_id == activation_id)
        )
    ).all()
    assert len(entries) == 1


async def test_a_rule_with_conditions_is_skipped(
    client: AsyncClient, service_headers, submitted, session, today
) -> None:
    """The condition language was never specified. Paying out on terms nobody agreed
    would be worse than paying nothing."""
    session.add(
        IncentiveRule(
            rule_name="Conditional bonus",
            event_type=IncentiveEventType.ACTIVATION,
            amount_idr=Decimal("75000.00"),
            conditions={"minPerMonth": 10},
            effective_from=today - timedelta(days=30),
        )
    )
    await session.flush()
    item, _, activation_id = submitted

    await client.post(
        "/internal/ga-events",
        json={"events": [{"msisdn": item.msisdn, "activationDate": datetime.now(UTC).isoformat()}]},
        headers=service_headers,
    )

    entries = (
        await session.scalars(
            select(IncentiveLedger).where(IncentiveLedger.activation_id == activation_id)
        )
    ).all()
    assert entries == []


async def test_a_rule_outside_its_effective_window_is_skipped(
    client: AsyncClient, service_headers, submitted, session, today
) -> None:
    session.add(
        IncentiveRule(
            rule_name="Expired bonus",
            event_type=IncentiveEventType.ACTIVATION,
            amount_idr=Decimal("50000.00"),
            effective_from=today - timedelta(days=400),
            effective_to=today - timedelta(days=200),
        )
    )
    await session.flush()
    item, _, activation_id = submitted

    await client.post(
        "/internal/ga-events",
        json={"events": [{"msisdn": item.msisdn, "activationDate": datetime.now(UTC).isoformat()}]},
        headers=service_headers,
    )

    entries = (
        await session.scalars(
            select(IncentiveLedger).where(IncentiveLedger.activation_id == activation_id)
        )
    ).all()
    assert entries == []


async def test_a_later_failure_voids_the_accrual(
    client: AsyncClient, service_headers, submitted, activation_rule, session
) -> None:
    item, _, activation_id = submitted
    when = datetime.now(UTC).isoformat()

    await client.post(
        "/internal/ga-events",
        json={"events": [{"msisdn": item.msisdn, "activationDate": when}]},
        headers=service_headers,
    )
    await client.post(
        "/internal/ga-events",
        json={"events": [{"msisdn": item.msisdn, "activationDate": when, "outcome": "FAILED"}]},
        headers=service_headers,
    )

    entries = (
        await session.scalars(
            select(IncentiveLedger).where(IncentiveLedger.activation_id == activation_id)
        )
    ).all()
    # Marked VOID rather than deleted, so a payout dispute is still reconstructable.
    assert [entry.status for entry in entries] == [LedgerStatus.VOID]
