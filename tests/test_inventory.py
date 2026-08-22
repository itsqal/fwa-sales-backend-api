"""Inventory lookup — the barcode scanner's server side."""

from __future__ import annotations

from httpx import AsyncClient

from app.models.enums import InventoryStatus, NetworkGeneration


async def test_lookup_resolves_the_bundled_device(
    client: AsyncClient, auth_headers, ae, make_inventory, device_model
) -> None:
    """This is what fills the read-only IMEI and Tipe Modem fields on the form."""
    item = await make_inventory(ae=ae)

    response = await client.get(f"/inventory/msisdn/{item.msisdn}", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["msisdn"] == item.msisdn
    assert body["imei"] == item.imei
    assert body["iccid"] == item.iccid
    assert body["deviceModel"]["modelCode"] == device_model.model_code
    assert body["eligible"] is True


async def test_lookup_exposes_the_device_network_generation(
    client: AsyncClient, auth_headers, ae, make_inventory, device_model, session
) -> None:
    """It drives the incentive tier, so the app shows the AE what a scan is worth."""
    device_model.network_generation = NetworkGeneration.FIVE_G
    await session.flush()
    item = await make_inventory(ae=ae)

    response = await client.get(f"/inventory/msisdn/{item.msisdn}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["deviceModel"]["networkGeneration"] == "5G"


async def test_reason_is_absent_rather_than_null_when_eligible(
    client: AsyncClient, auth_headers, ae, make_inventory
) -> None:
    """The contract types `reason` as a plain string enum and says it is "populated only
    when eligible is false" — so an eligible unit omits the key. Emitting null puts a
    value in the field that the declared schema rejects."""
    item = await make_inventory(ae=ae)

    single = await client.get(f"/inventory/msisdn/{item.msisdn}", headers=auth_headers)
    listed = await client.get("/inventory/me", headers=auth_headers)
    bootstrap = await client.get("/sync/bootstrap", headers=auth_headers)

    assert "reason" not in single.json()
    assert "reason" not in listed.json()["data"][0]
    assert "reason" not in bootstrap.json()["inventory"][0]


async def test_reason_is_present_when_not_eligible(
    client: AsyncClient, auth_headers, other_ae, make_inventory
) -> None:
    item = await make_inventory(ae=other_ae)

    response = await client.get(f"/inventory/msisdn/{item.msisdn}", headers=auth_headers)

    assert response.json()["reason"] == "NOT_ALLOCATED_TO_YOU"


async def test_lookup_accepts_an_unnormalised_08xx_scan(
    client: AsyncClient, auth_headers, ae, make_inventory
) -> None:
    """A barcode may scan as 08xx; it is normalised at the edge, once."""
    item = await make_inventory(ae=ae)
    local_form = "0" + item.msisdn[2:]

    response = await client.get(f"/inventory/msisdn/{local_form}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["msisdn"] == item.msisdn


async def test_lookup_of_an_unknown_msisdn_is_404(client: AsyncClient, auth_headers) -> None:
    response = await client.get("/inventory/msisdn/6289999999999", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MSISDN_NOT_FOUND"


async def test_lookup_of_an_unparseable_msisdn_is_404(client: AsyncClient, auth_headers) -> None:
    response = await client.get("/inventory/msisdn/abcdefg", headers=auth_headers)

    assert response.status_code == 404


async def test_lookup_requires_auth(client: AsyncClient, ae, make_inventory) -> None:
    item = await make_inventory(ae=ae)

    assert (await client.get(f"/inventory/msisdn/{item.msisdn}")).status_code == 401


async def test_another_aes_unit_is_visible_but_not_eligible(
    client: AsyncClient, auth_headers, other_ae, make_inventory
) -> None:
    item = await make_inventory(ae=other_ae)

    response = await client.get(f"/inventory/msisdn/{item.msisdn}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["eligible"] is False
    assert response.json()["reason"] == "NOT_ALLOCATED_TO_YOU"


async def test_blocked_and_returned_units_are_not_eligible(
    client: AsyncClient, auth_headers, ae, make_inventory
) -> None:
    blocked = await make_inventory(ae=ae, status=InventoryStatus.BLOCKED)
    returned = await make_inventory(ae=ae, status=InventoryStatus.RETURNED)

    blocked_response = await client.get(f"/inventory/msisdn/{blocked.msisdn}", headers=auth_headers)
    returned_response = await client.get(
        f"/inventory/msisdn/{returned.msisdn}", headers=auth_headers
    )

    assert blocked_response.json()["reason"] == "BLOCKED"
    assert returned_response.json()["reason"] == "RETURNED"


async def test_a_sold_unit_reports_already_activated(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer
) -> None:
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)
    await client.post(
        "/activations",
        json={
            "customerId": customer.customer_id,
            "msisdn": item.msisdn,
            "latitude": -6.2,
            "longitude": 106.9,
        },
        headers=auth_headers,
    )

    response = await client.get(f"/inventory/msisdn/{item.msisdn}", headers=auth_headers)

    assert response.json()["eligible"] is False
    assert response.json()["reason"] == "ALREADY_ACTIVATED"


async def test_my_stock_lists_only_this_aes_units(
    client: AsyncClient, auth_headers, ae, other_ae, make_inventory
) -> None:
    await make_inventory(ae=ae)
    await make_inventory(ae=ae)
    await make_inventory(ae=other_ae)

    response = await client.get("/inventory/me", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["meta"]["total"] == 2
    assert all(row["eligible"] for row in response.json()["data"])


async def test_my_stock_filters_by_status(
    client: AsyncClient, auth_headers, ae, make_inventory
) -> None:
    await make_inventory(ae=ae, status=InventoryStatus.ALLOCATED)
    await make_inventory(ae=ae, status=InventoryStatus.BLOCKED)

    response = await client.get("/inventory/me?status=BLOCKED", headers=auth_headers)

    assert response.json()["meta"]["total"] == 1


async def test_my_stock_rejects_an_unknown_status(client: AsyncClient, auth_headers) -> None:
    response = await client.get("/inventory/me?status=NONSENSE", headers=auth_headers)

    assert response.status_code == 422
