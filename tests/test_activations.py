"""Activation submission — the write path where fraud and duplication would live."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.activation import Activation
from app.models.enums import ActivationStatus, InventoryStatus


def body(customer_id: int, msisdn: str, **overrides) -> dict:
    payload = {
        "customerId": customer_id,
        "msisdn": msisdn,
        "latitude": -6.2314728,
        "longitude": 106.9348415,
        "geoAccuracyM": 8.0,
        "isMocked": False,
    }
    payload.update(overrides)
    return payload


async def test_create_activation_snapshots_the_device_from_inventory(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer, device_model
) -> None:
    """The client never asserts IMEI or modem type; the server reads them from stock."""
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)

    response = await client.post(
        "/activations", json=body(customer.customer_id, item.msisdn), headers=auth_headers
    )

    assert response.status_code == 201
    result = response.json()
    assert result["activationId"] >= 2_900_000_000
    assert result["imei"] == item.imei
    assert result["deviceModelCode"] == device_model.model_code
    assert result["customer"] == {"customerId": customer.customer_id, "fullName": "Susanto"}
    assert result["status"] == "NOT_ACTIVATED"
    assert result["activationDate"] is None


async def test_client_supplied_imei_is_ignored(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer
) -> None:
    """Trusting a client-supplied IMEI would let an AE fabricate inventory."""
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)

    response = await client.post(
        "/activations",
        json=body(customer.customer_id, item.msisdn, imei="999999999999999"),
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert response.json()["imei"] == item.imei


async def test_activation_consumes_the_unit(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer, session
) -> None:
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)

    await client.post(
        "/activations", json=body(customer.customer_id, item.msisdn), headers=auth_headers
    )

    await session.refresh(item)
    assert item.status is InventoryStatus.CONSUMED


async def test_activating_the_same_msisdn_twice_is_409(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer
) -> None:
    item = await make_inventory(ae=ae)
    first_customer = await make_customer(ae=ae)
    second_customer = await make_customer(ae=ae)

    first = await client.post(
        "/activations", json=body(first_customer.customer_id, item.msisdn), headers=auth_headers
    )
    second = await client.post(
        "/activations", json=body(second_customer.customer_id, item.msisdn), headers=auth_headers
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "MSISDN_ALREADY_ACTIVATED"


async def test_activating_another_aes_msisdn_is_422_not_allocated(
    client: AsyncClient, auth_headers, ae, other_ae, make_inventory, make_customer
) -> None:
    theirs = await make_inventory(ae=other_ae)
    customer = await make_customer(ae=ae)

    response = await client.post(
        "/activations", json=body(customer.customer_id, theirs.msisdn), headers=auth_headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MSISDN_NOT_ALLOCATED"


async def test_activating_unallocated_stock_is_422(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer
) -> None:
    unallocated = await make_inventory(ae=None, status=InventoryStatus.AVAILABLE)
    customer = await make_customer(ae=ae)

    response = await client.post(
        "/activations", json=body(customer.customer_id, unallocated.msisdn), headers=auth_headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MSISDN_NOT_ALLOCATED"


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [(InventoryStatus.BLOCKED, "MSISDN_BLOCKED"), (InventoryStatus.RETURNED, "MSISDN_RETURNED")],
)
async def test_ineligible_stock_is_rejected(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer, status, expected_code
) -> None:
    item = await make_inventory(ae=ae, status=status)
    customer = await make_customer(ae=ae)

    response = await client.post(
        "/activations", json=body(customer.customer_id, item.msisdn), headers=auth_headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code


async def test_activating_for_another_aes_customer_is_422(
    client: AsyncClient, auth_headers, ae, other_ae, make_inventory, make_customer
) -> None:
    item = await make_inventory(ae=ae)
    theirs = await make_customer(ae=other_ae)

    response = await client.post(
        "/activations", json=body(theirs.customer_id, item.msisdn), headers=auth_headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CUSTOMER_NOT_OWNED"


async def test_unknown_msisdn_is_422(client: AsyncClient, auth_headers, ae, make_customer) -> None:
    customer = await make_customer(ae=ae)

    response = await client.post(
        "/activations", json=body(customer.customer_id, "6289999999999"), headers=auth_headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MSISDN_NOT_FOUND"


async def test_create_activation_requires_auth(client: AsyncClient) -> None:
    assert (await client.post("/activations", json=body(1, "6285882724305"))).status_code == 401


@pytest.mark.parametrize(
    ("field", "value"),
    [("msisdn", "not-a-number"), ("latitude", 999.0), ("customerId", "abc")],
)
async def test_create_activation_validation(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer, field, value
) -> None:
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)
    request = body(customer.customer_id, item.msisdn) | {field: value}

    response = await client.post("/activations", json=request, headers=auth_headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_msisdn_is_normalised_before_lookup(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer
) -> None:
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)
    local_form = "0" + item.msisdn[2:]

    response = await client.post(
        "/activations", json=body(customer.customer_id, local_form), headers=auth_headers
    )

    assert response.status_code == 201
    assert response.json()["msisdn"] == item.msisdn


async def test_idempotency_replay_creates_no_second_row(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer, session
) -> None:
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)
    headers = {**auth_headers, "Idempotency-Key": str(uuid.uuid4())}
    request = body(customer.customer_id, item.msisdn)

    first = await client.post("/activations", json=request, headers=headers)
    second = await client.post("/activations", json=request, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json()["activationId"] == second.json()["activationId"]

    rows = (await session.scalars(select(Activation).where(Activation.msisdn == item.msisdn))).all()
    assert len(rows) == 1


async def test_mocked_gps_is_recorded_not_rejected(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer
) -> None:
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)

    response = await client.post(
        "/activations",
        json=body(customer.customer_id, item.msisdn, isMocked=True),
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert response.json()["geo"]["isMocked"] is True
    assert response.json()["geo"]["verified"] is False


async def test_device_time_does_not_set_submitted_at(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer
) -> None:
    """Field phones have wrong clocks. The server timestamps its own rows."""
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)

    response = await client.post(
        "/activations",
        json=body(customer.customer_id, item.msisdn, deviceTime="2019-01-01T00:00:00+07:00"),
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert not response.json()["submittedAt"].startswith("2019")


async def test_list_is_scoped_to_the_signed_in_ae(
    client: AsyncClient,
    auth_headers,
    other_auth_headers,
    ae,
    other_ae,
    make_inventory,
    make_customer,
) -> None:
    mine = await make_inventory(ae=ae)
    my_customer = await make_customer(ae=ae)
    await client.post(
        "/activations", json=body(my_customer.customer_id, mine.msisdn), headers=auth_headers
    )

    theirs = await make_inventory(ae=other_ae)
    their_customer = await make_customer(ae=other_ae)
    await client.post(
        "/activations",
        json=body(their_customer.customer_id, theirs.msisdn),
        headers=other_auth_headers,
    )

    listed = await client.get("/activations", headers=auth_headers)

    assert listed.status_code == 200
    assert listed.json()["meta"]["total"] == 1
    assert listed.json()["data"][0]["msisdn"] == mine.msisdn


async def test_list_filters_by_status(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer
) -> None:
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)
    await client.post(
        "/activations", json=body(customer.customer_id, item.msisdn), headers=auth_headers
    )

    not_activated = await client.get("/activations?status=NOT_ACTIVATED", headers=auth_headers)
    activated = await client.get("/activations?status=ACTIVATED", headers=auth_headers)

    assert not_activated.json()["meta"]["total"] == 1
    assert activated.json()["meta"]["total"] == 0


async def test_get_one_activation(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer
) -> None:
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)
    created = await client.post(
        "/activations", json=body(customer.customer_id, item.msisdn), headers=auth_headers
    )
    activation_id = created.json()["activationId"]

    response = await client.get(f"/activations/{activation_id}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["activationId"] == activation_id


async def test_get_another_aes_activation_is_404(
    client: AsyncClient, auth_headers, other_auth_headers, other_ae, make_inventory, make_customer
) -> None:
    item = await make_inventory(ae=other_ae)
    customer = await make_customer(ae=other_ae)
    created = await client.post(
        "/activations", json=body(customer.customer_id, item.msisdn), headers=other_auth_headers
    )

    response = await client.get(
        f"/activations/{created.json()['activationId']}", headers=auth_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ACTIVATION_NOT_FOUND"


async def test_no_ae_facing_path_can_set_activation_date(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer, session
) -> None:
    """activation_date belongs to the GA feed alone — it gates the badge and the money."""
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)

    await client.post(
        "/activations",
        json=body(
            customer.customer_id,
            item.msisdn,
            activationDate="2026-08-11T09:00:00+07:00",
            status="ACTIVATED",
        ),
        headers=auth_headers,
    )

    stored = await session.scalar(select(Activation).where(Activation.msisdn == item.msisdn))
    assert stored.activation_date is None
    assert stored.status is ActivationStatus.NOT_ACTIVATED
