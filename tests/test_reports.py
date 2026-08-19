"""Report endpoints: the Home tiles, the daily chart, and the incentive ledger."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.models.enums import CustomerStatus, IncentiveEventType, LedgerStatus
from app.models.incentive import AeDailyTarget, IncentiveLedger, IncentiveRule


async def _submit(client, headers, customer_id: int, msisdn: str) -> int:
    response = await client.post(
        "/activations",
        json={"customerId": customer_id, "msisdn": msisdn, "latitude": -6.2, "longitude": 106.9},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["activationId"]


async def test_summary_counts_the_four_tiles(
    client: AsyncClient, auth_headers, ae, make_customer, make_inventory, today
) -> None:
    purchase = await make_customer(ae=ae, status=CustomerStatus.PURCHASE, visit_date=today)
    await make_customer(ae=ae, status=CustomerStatus.HOT_LEADS, visit_date=today)
    await make_customer(ae=ae, status=CustomerStatus.EDUKASI, visit_date=today)
    item = await make_inventory(ae=ae)
    await _submit(client, auth_headers, purchase.customer_id, item.msisdn)

    response = await client.get("/reports/summary", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["totalActivations"] == 1
    assert data["activatedCount"] == 0
    assert data["notActivatedCount"] == 1
    assert data["newCustomers"] == 3
    assert data["hotLeads"] == 1
    assert data["periodFrom"] == (today - timedelta(days=6)).isoformat()
    assert data["periodTo"] == today.isoformat()


async def test_summary_activated_count_follows_the_ga_feed(
    client: AsyncClient, auth_headers, service_headers, ae, make_customer, make_inventory, today
) -> None:
    customer = await make_customer(ae=ae, visit_date=today)
    item = await make_inventory(ae=ae)
    await _submit(client, auth_headers, customer.customer_id, item.msisdn)

    await client.post(
        "/internal/ga-events",
        json={"events": [{"msisdn": item.msisdn, "activationDate": datetime.now(UTC).isoformat()}]},
        headers=service_headers,
    )

    data = (await client.get("/reports/summary", headers=auth_headers)).json()["data"]
    assert data["activatedCount"] == 1
    assert data["notActivatedCount"] == 0
    assert data["conversionRate"] == 1.0


async def test_summary_conversion_rate_is_zero_when_there_are_no_customers(
    client: AsyncClient, auth_headers
) -> None:
    """No customers must give 0.0, not a division-by-zero 500."""
    data = (await client.get("/reports/summary", headers=auth_headers)).json()["data"]

    assert data["conversionRate"] == 0.0
    assert data["newCustomers"] == 0


async def test_summary_incentive_is_zero_without_rules(client: AsyncClient, auth_headers) -> None:
    data = (await client.get("/reports/summary", headers=auth_headers)).json()["data"]

    assert data["incentiveIdr"] == 0


async def test_summary_incentive_is_whole_rupiah(
    client: AsyncClient, auth_headers, ae, session, today
) -> None:
    """Money is an integer of whole rupiah at the edge, never a float."""
    session.add(
        IncentiveLedger(
            ae_id=ae.ae_id,
            amount_idr=Decimal("24200000.00"),
            earned_date=today,
            period_ym=today.strftime("%Y-%m"),
            status=LedgerStatus.ACCRUED,
        )
    )
    await session.flush()

    data = (await client.get("/reports/summary", headers=auth_headers)).json()["data"]

    assert data["incentiveIdr"] == 24_200_000
    assert isinstance(data["incentiveIdr"], int)


async def test_voided_ledger_entries_are_excluded_from_the_tile(
    client: AsyncClient, auth_headers, ae, session, today
) -> None:
    session.add_all(
        [
            IncentiveLedger(
                ae_id=ae.ae_id,
                amount_idr=Decimal("50000.00"),
                earned_date=today,
                period_ym=today.strftime("%Y-%m"),
                status=LedgerStatus.ACCRUED,
            ),
            IncentiveLedger(
                ae_id=ae.ae_id,
                amount_idr=Decimal("50000.00"),
                earned_date=today,
                period_ym=today.strftime("%Y-%m"),
                status=LedgerStatus.VOID,
            ),
        ]
    )
    await session.flush()

    data = (await client.get("/reports/summary", headers=auth_headers)).json()["data"]

    assert data["incentiveIdr"] == 50_000


async def test_summary_is_scoped_to_the_signed_in_ae(
    client: AsyncClient, auth_headers, other_ae, make_customer, today
) -> None:
    await make_customer(ae=other_ae, visit_date=today)

    data = (await client.get("/reports/summary", headers=auth_headers)).json()["data"]

    assert data["newCustomers"] == 0


async def test_summary_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/reports/summary")).status_code == 401


async def test_daily_activity_has_no_gaps_across_quiet_days(
    client: AsyncClient, auth_headers, ae, make_customer, today
) -> None:
    """A quiet week must still render seven bars, not collapse the axis."""
    await make_customer(ae=ae, visit_date=today)

    response = await client.get("/reports/daily-activity", headers=auth_headers)

    assert response.status_code == 200
    series = response.json()["data"]
    assert len(series) == 7

    dates = [row["date"] for row in series]
    expected = [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    assert dates == expected
    assert sum(row["newCustomers"] for row in series) == 1


async def test_daily_activity_over_a_completely_empty_range(
    client: AsyncClient, auth_headers, today
) -> None:
    start = today - timedelta(days=20)
    end = today - timedelta(days=15)

    response = await client.get(
        f"/reports/daily-activity?from={start.isoformat()}&to={end.isoformat()}",
        headers=auth_headers,
    )

    series = response.json()["data"]
    assert len(series) == 6
    assert all(row["activations"] == 0 and row["newCustomers"] == 0 for row in series)


async def test_daily_activity_includes_the_target_bars(
    client: AsyncClient, auth_headers, ae, session, today
) -> None:
    session.add(AeDailyTarget(ae_id=ae.ae_id, target_date=today, target_activations=4))
    await session.flush()

    series = (await client.get("/reports/daily-activity", headers=auth_headers)).json()["data"]

    assert series[-1]["target"] == 4
    assert series[0]["target"] == 0


async def test_daily_activity_counts_activations_on_the_submission_day(
    client: AsyncClient, auth_headers, ae, make_customer, make_inventory, today
) -> None:
    customer = await make_customer(ae=ae, visit_date=today)
    item = await make_inventory(ae=ae)
    await _submit(client, auth_headers, customer.customer_id, item.msisdn)

    series = (await client.get("/reports/daily-activity", headers=auth_headers)).json()["data"]

    assert series[-1]["date"] == today.isoformat()
    assert series[-1]["activations"] == 1


@pytest.mark.parametrize(
    "path",
    [
        "/reports/summary",
        "/reports/daily-activity",
        "/customers",
        "/activations",
        "/attendance",
    ],
)
async def test_invalid_date_range_is_422(
    client: AsyncClient, auth_headers, today, path: str
) -> None:
    """Both dates are individually valid, so nothing rejects the pair until the server
    compares them. Documented in the contract as DateRangeError."""
    response = await client.get(
        f"{path}?from={today.isoformat()}&to={(today - timedelta(days=5)).isoformat()}",
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_DATE_RANGE"


@pytest.mark.parametrize("period", ["7d", "30d", "mtd", "ytd"])
async def test_every_declared_period_is_accepted(client: AsyncClient, auth_headers, period) -> None:
    response = await client.get(f"/reports/summary?period={period}", headers=auth_headers)

    assert response.status_code == 200


async def test_unknown_period_is_422(client: AsyncClient, auth_headers) -> None:
    assert (
        await client.get("/reports/summary?period=all-time", headers=auth_headers)
    ).status_code == 422


async def test_incentive_ledger_lists_entries(
    client: AsyncClient, auth_headers, ae, session, today
) -> None:
    rule = IncentiveRule(
        rule_name="Activation bonus — test",
        event_type=IncentiveEventType.ACTIVATION,
        amount_idr=Decimal("50000.00"),
        effective_from=today,
    )
    session.add(rule)
    await session.flush()
    session.add(
        IncentiveLedger(
            ae_id=ae.ae_id,
            rule_id=rule.rule_id,
            amount_idr=Decimal("50000.00"),
            earned_date=today,
            period_ym=today.strftime("%Y-%m"),
        )
    )
    await session.flush()

    response = await client.get("/reports/incentives", headers=auth_headers)

    assert response.status_code == 200
    entry = response.json()["data"][0]
    assert entry["ruleName"] == "Activation bonus — test"
    assert entry["amountIdr"] == 50_000
    assert entry["periodYm"] == today.strftime("%Y-%m")
    assert entry["status"] == "ACCRUED"


async def test_incentive_ledger_filters_by_period(
    client: AsyncClient, auth_headers, ae, session, today
) -> None:
    session.add(
        IncentiveLedger(
            ae_id=ae.ae_id,
            amount_idr=Decimal("50000.00"),
            earned_date=today,
            period_ym=today.strftime("%Y-%m"),
        )
    )
    await session.flush()

    match = await client.get(
        f"/reports/incentives?periodYm={today.strftime('%Y-%m')}", headers=auth_headers
    )
    miss = await client.get("/reports/incentives?periodYm=1999-01", headers=auth_headers)

    assert match.json()["meta"]["total"] == 1
    assert miss.json()["meta"]["total"] == 0


async def test_incentive_ledger_rejects_a_malformed_period(
    client: AsyncClient, auth_headers
) -> None:
    response = await client.get("/reports/incentives?periodYm=August", headers=auth_headers)

    assert response.status_code == 422


async def test_incentive_ledger_is_scoped_to_the_signed_in_ae(
    client: AsyncClient, auth_headers, other_ae, session, today
) -> None:
    session.add(
        IncentiveLedger(
            ae_id=other_ae.ae_id,
            amount_idr=Decimal("50000.00"),
            earned_date=today,
            period_ym=today.strftime("%Y-%m"),
        )
    )
    await session.flush()

    response = await client.get("/reports/incentives", headers=auth_headers)

    assert response.json()["meta"]["total"] == 0
