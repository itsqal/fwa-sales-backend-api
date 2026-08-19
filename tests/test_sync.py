"""Offline bootstrap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.models.enums import InventoryStatus


async def test_bootstrap_returns_everything_the_offline_app_needs(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer
) -> None:
    await make_inventory(ae=ae)
    await make_customer(ae=ae)

    response = await client.get("/sync/bootstrap", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["profile"]["aeCode"] == ae.ae_code
    assert len(body["inventory"]) == 1
    assert len(body["customers"]) == 1
    assert body["serverTime"]


async def test_bootstrap_server_time_is_the_server_clock(client: AsyncClient, auth_headers) -> None:
    """The device corrects its own drift against this, so it must be current."""
    response = await client.get("/sync/bootstrap", headers=auth_headers)

    server_time = datetime.fromisoformat(response.json()["serverTime"])
    assert abs((datetime.now(UTC) - server_time).total_seconds()) < 60


async def test_bootstrap_is_scoped_to_the_signed_in_ae(
    client: AsyncClient, auth_headers, other_ae, make_inventory, make_customer
) -> None:
    await make_inventory(ae=other_ae)
    await make_customer(ae=other_ae)

    response = await client.get("/sync/bootstrap", headers=auth_headers)

    assert response.json()["inventory"] == []
    assert response.json()["customers"] == []


async def test_bootstrap_omits_consumed_stock(
    client: AsyncClient, auth_headers, ae, make_inventory
) -> None:
    """A sold unit is not sellable, so shipping it to the offline cache would let the
    app offer a box that is already gone."""
    await make_inventory(ae=ae, status=InventoryStatus.CONSUMED)

    response = await client.get("/sync/bootstrap", headers=auth_headers)

    assert response.json()["inventory"] == []


async def test_since_filters_customers(
    client: AsyncClient, auth_headers, ae, make_customer
) -> None:
    await make_customer(ae=ae)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    # Passed as a param, not interpolated: the "+00:00" offset decodes as a space in a
    # raw query string, which is a real trap for the mobile client too.
    response = await client.get("/sync/bootstrap", params={"since": future}, headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["customers"] == []


async def test_since_in_the_past_still_returns_customers(
    client: AsyncClient, auth_headers, ae, make_customer
) -> None:
    await make_customer(ae=ae)
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    response = await client.get("/sync/bootstrap", params={"since": past}, headers=auth_headers)

    assert len(response.json()["customers"]) == 1


async def test_bootstrap_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/sync/bootstrap")).status_code == 401
