"""Gross Add ingestion — the only path allowed to write activation_date."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.activation import Activation
from app.models.enums import (
    ActivationStatus,
    IncentiveEventType,
    InventoryStatus,
    LedgerStatus,
    NetworkGeneration,
)
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


async def test_an_uncategorised_device_model_accrues_nothing(
    client: AsyncClient, service_headers, submitted, session
) -> None:
    """Both shipped tiers are keyed on 4G/5G, and the fixture model is uncategorised.

    Accruing nothing is the recoverable failure: fix the ``device_model`` row and
    re-deliver the GA event. Defaulting an unknown model into the base tier would pay
    money on a guess.
    """
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


async def test_a_rule_with_an_unsupported_condition_is_skipped(
    client: AsyncClient, service_headers, submitted, session, today
) -> None:
    """Volume tiering is still undecided, so `minPerMonth` has no defined semantics.

    Paying out on terms nobody agreed to would be worse than paying nothing.
    """
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


# ---------------------------------------------------------------------------
# The 4G / 5G activation tiers agreed with the business on 2026-08-22.
# These read the rules migration 0002 actually installs rather than inventing their
# own: a test that created its own Rp 35.000 row would stay green even if the
# migration shipped the wrong number.
# ---------------------------------------------------------------------------


@pytest.fixture
async def tier_rules(session) -> dict[str, IncentiveRule]:
    rules = (
        await session.scalars(
            select(IncentiveRule).where(
                IncentiveRule.event_type == IncentiveEventType.ACTIVATION,
                IncentiveRule.conditions.has_key("networkGeneration"),
            )
        )
    ).all()
    by_generation = {rule.conditions["networkGeneration"]: rule for rule in rules}
    assert set(by_generation) == {"4G", "5G"}, "migration 0002 must supply both tiers"
    return by_generation


@pytest.mark.parametrize(
    ("generation", "expected_idr"),
    [
        (NetworkGeneration.FOUR_G, Decimal("35000.00")),
        (NetworkGeneration.FIVE_G, Decimal("135000.00")),
    ],
)
async def test_a_confirmed_ga_accrues_the_tier_of_its_modem(
    client: AsyncClient,
    service_headers,
    submitted,
    device_model,
    session,
    tier_rules,
    generation: NetworkGeneration,
    expected_idr: Decimal,
) -> None:
    device_model.network_generation = generation
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
    # Exactly one: the other tier's rule must not also match.
    assert len(entries) == 1
    assert entries[0].amount_idr == expected_idr
    assert entries[0].rule_id == tier_rules[generation.value].rule_id


async def test_a_failed_ga_voids_a_tier_accrual(
    client: AsyncClient, service_headers, submitted, device_model, session, tier_rules
) -> None:
    """A unit that goes live and is then reported failed must not stay on the payroll."""
    device_model.network_generation = NetworkGeneration.FIVE_G
    await session.flush()
    item, _, activation_id = submitted

    await client.post(
        "/internal/ga-events",
        json={"events": [{"msisdn": item.msisdn, "activationDate": datetime.now(UTC).isoformat()}]},
        headers=service_headers,
    )
    await client.post(
        "/internal/ga-events",
        json={
            "events": [
                {
                    "msisdn": item.msisdn,
                    "activationDate": datetime.now(UTC).isoformat(),
                    "outcome": "FAILED",
                }
            ]
        },
        headers=service_headers,
    )

    entries = (
        await session.scalars(
            select(IncentiveLedger).where(IncentiveLedger.activation_id == activation_id)
        )
    ).all()
    assert [entry.status for entry in entries] == [LedgerStatus.VOID]
