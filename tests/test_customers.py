"""Customer registration, lists, funnel movement, and AE scoping."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.models.enums import CustomerStatus


def payload(**overrides) -> dict:
    body = {
        "visitDate": "2026-08-19",
        "fullName": "Susanto",
        "phoneNumber": "082234567890",
        "address": "Jl. Kebahagiaan No. 12",
        "latitude": -6.2314728,
        "longitude": 106.9348415,
        "geoAccuracyM": 12.5,
        "isMocked": False,
        "status": "PURCHASE",
    }
    body.update(overrides)
    return body


async def test_create_customer(client: AsyncClient, auth_headers, today) -> None:
    response = await client.post(
        "/customers", json=payload(visitDate=today.isoformat()), headers=auth_headers
    )

    assert response.status_code == 201
    body = response.json()
    # The identity sequence starts at 1000000000 so the "ID Customer" the UI shows is
    # genuinely ten digits.
    assert body["customerId"] >= 1_000_000_000
    assert body["fullName"] == "Susanto"
    assert body["hasActivation"] is False
    assert body["geo"]["verified"] is True
    assert body["geo"]["isMocked"] is False


async def test_create_customer_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/customers", json=payload())

    assert response.status_code == 401


async def test_ae_id_cannot_be_supplied_by_the_client(
    client: AsyncClient, auth_headers, other_ae, session
) -> None:
    """A rogue aeId in the body must be ignored, not honoured."""
    response = await client.post(
        "/customers", json=payload(aeId=str(other_ae.ae_id)), headers=auth_headers
    )

    assert response.status_code == 201
    from app.models.customer import Customer

    stored = await session.get(Customer, response.json()["customerId"])
    assert stored.ae_id != other_ae.ae_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phoneNumber", "12345"),
        ("phoneNumber", "not-a-number"),
        ("latitude", 120.0),
        ("longitude", -200.0),
        ("status", "POTENTIAL"),  # the rejected Solution-Description taxonomy
        ("visitDate", "not-a-date"),
    ],
)
async def test_create_customer_validation(client: AsyncClient, auth_headers, field, value) -> None:
    response = await client.post("/customers", json=payload(**{field: value}), headers=auth_headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


async def test_duplicate_phone_for_the_same_ae_is_409(client: AsyncClient, auth_headers) -> None:
    await client.post("/customers", json=payload(), headers=auth_headers)

    response = await client.post(
        "/customers", json=payload(fullName="Someone Else"), headers=auth_headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CUSTOMER_ALREADY_REGISTERED"


async def test_the_same_phone_may_be_registered_by_a_different_ae(
    client: AsyncClient, auth_headers, other_auth_headers
) -> None:
    """The uniqueness rule is per AE — two AEs meeting the same household is not an error."""
    first = await client.post("/customers", json=payload(), headers=auth_headers)
    second = await client.post("/customers", json=payload(), headers=other_auth_headers)

    assert first.status_code == 201
    assert second.status_code == 201


async def test_mocked_location_is_flagged_not_rejected(client: AsyncClient, auth_headers) -> None:
    """Fake GPS is a fraud vector, but a device quirk must not block a real AE."""
    response = await client.post("/customers", json=payload(isMocked=True), headers=auth_headers)

    assert response.status_code == 201
    assert response.json()["geo"]["isMocked"] is True
    assert response.json()["geo"]["verified"] is False


async def test_idempotency_key_replay_returns_the_original(
    client: AsyncClient, auth_headers
) -> None:
    key = str(uuid.uuid4())
    headers = {**auth_headers, "Idempotency-Key": key}

    first = await client.post("/customers", json=payload(), headers=headers)
    second = await client.post("/customers", json=payload(), headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json()["customerId"] == second.json()["customerId"]
    assert second.headers.get("Idempotent-Replay") == "true"

    listed = await client.get("/customers", headers=auth_headers)
    assert listed.json()["meta"]["total"] == 1


async def test_idempotency_key_reused_with_a_different_payload_is_409(
    client: AsyncClient, auth_headers
) -> None:
    key = str(uuid.uuid4())
    headers = {**auth_headers, "Idempotency-Key": key}

    await client.post("/customers", json=payload(), headers=headers)
    response = await client.post(
        "/customers",
        json=payload(phoneNumber="081200000001", fullName="Different"),
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_malformed_idempotency_key_is_422(client: AsyncClient, auth_headers) -> None:
    response = await client.post(
        "/customers", json=payload(), headers={**auth_headers, "Idempotency-Key": "not-a-uuid"}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_IDEMPOTENCY_KEY"


async def test_list_is_scoped_to_the_signed_in_ae(
    client: AsyncClient, auth_headers, ae, other_ae, make_customer
) -> None:
    await make_customer(ae=ae)
    await make_customer(ae=other_ae)

    response = await client.get("/customers", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["meta"]["total"] == 1


async def test_list_filters_by_status(client: AsyncClient, auth_headers, ae, make_customer) -> None:
    """The Hot Leads screen is this endpoint with status=HOT_LEADS."""
    await make_customer(ae=ae, status=CustomerStatus.HOT_LEADS)
    await make_customer(ae=ae, status=CustomerStatus.EDUKASI)

    response = await client.get("/customers?status=HOT_LEADS", headers=auth_headers)

    assert response.json()["meta"]["total"] == 1
    assert response.json()["data"][0]["status"] == "HOT_LEADS"


async def test_list_period_window_excludes_older_visits(
    client: AsyncClient, auth_headers, ae, make_customer, today
) -> None:
    await make_customer(ae=ae, visit_date=today)
    await make_customer(ae=ae, visit_date=today - timedelta(days=40))

    default_window = await client.get("/customers", headers=auth_headers)
    wide_window = await client.get("/customers?period=ytd", headers=auth_headers)

    assert default_window.json()["meta"]["total"] == 1
    assert wide_window.json()["meta"]["total"] == 2


async def test_explicit_dates_override_period(
    client: AsyncClient, auth_headers, ae, make_customer, today
) -> None:
    old = today - timedelta(days=40)
    await make_customer(ae=ae, visit_date=old)

    response = await client.get(
        f"/customers?period=7d&from={old.isoformat()}&to={today.isoformat()}", headers=auth_headers
    )

    assert response.json()["meta"]["total"] == 1


async def test_list_searches_name_and_phone(
    client: AsyncClient, auth_headers, ae, make_customer
) -> None:
    customer = await make_customer(ae=ae)

    by_name = await client.get("/customers?q=Susan", headers=auth_headers)
    by_phone = await client.get(f"/customers?q={customer.phone_number[-5:]}", headers=auth_headers)
    miss = await client.get("/customers?q=zzzznotfound", headers=auth_headers)

    assert by_name.json()["meta"]["total"] == 1
    assert by_phone.json()["meta"]["total"] == 1
    assert miss.json()["meta"]["total"] == 0


async def test_pagination_meta(client: AsyncClient, auth_headers, ae, make_customer) -> None:
    for _ in range(3):
        await make_customer(ae=ae)

    response = await client.get("/customers?page=2&perPage=2", headers=auth_headers)

    meta = response.json()["meta"]
    assert meta == {"page": 2, "perPage": 2, "total": 3, "totalPages": 2}
    assert len(response.json()["data"]) == 1


async def test_get_another_aes_customer_is_404_not_403(
    client: AsyncClient, auth_headers, other_ae, make_customer
) -> None:
    """403 would confirm the record exists. It must not."""
    theirs = await make_customer(ae=other_ae)

    response = await client.get(f"/customers/{theirs.customer_id}", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CUSTOMER_NOT_FOUND"


async def test_patch_another_aes_customer_is_404(
    client: AsyncClient, auth_headers, other_ae, make_customer
) -> None:
    theirs = await make_customer(ae=other_ae)

    response = await client.patch(
        f"/customers/{theirs.customer_id}", json={"status": "PURCHASE"}, headers=auth_headers
    )

    assert response.status_code == 404


async def test_status_change_is_written_to_history(
    client: AsyncClient, auth_headers, ae, make_customer
) -> None:
    """This is what makes Hot Leads → Purchase conversion measurable later."""
    customer = await make_customer(ae=ae, status=CustomerStatus.HOT_LEADS)

    response = await client.patch(
        f"/customers/{customer.customer_id}",
        json={"status": "PURCHASE", "statusNote": "Agreed after second visit"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PURCHASE"
    latest = body["statusHistory"][0]
    assert latest["oldStatus"] == "HOT_LEADS"
    assert latest["newStatus"] == "PURCHASE"
    assert latest["note"] == "Agreed after second visit"


async def test_patch_without_a_status_change_writes_no_history(
    client: AsyncClient, auth_headers, ae, make_customer
) -> None:
    customer = await make_customer(ae=ae, status=CustomerStatus.HOT_LEADS)

    response = await client.patch(
        f"/customers/{customer.customer_id}",
        json={"address": "Jl. Baru No. 1"},
        headers=auth_headers,
    )

    assert response.json()["address"] == "Jl. Baru No. 1"
    assert response.json()["statusHistory"] == []


async def test_patch_validation_failure(
    client: AsyncClient, auth_headers, ae, make_customer
) -> None:
    customer = await make_customer(ae=ae)

    response = await client.patch(
        f"/customers/{customer.customer_id}", json={"phoneNumber": "abc"}, headers=auth_headers
    )

    assert response.status_code == 422


async def test_lookup_labels_and_excludes_activated(
    client: AsyncClient, auth_headers, ae, make_customer, make_inventory
) -> None:
    plain = await make_customer(ae=ae)
    activated = await make_customer(ae=ae)
    item = await make_inventory(ae=ae)
    created = await client.post(
        "/activations",
        json={
            "customerId": activated.customer_id,
            "msisdn": item.msisdn,
            "latitude": -6.2,
            "longitude": 106.9,
        },
        headers=auth_headers,
    )
    assert created.status_code == 201

    default = await client.get("/customers/lookup", headers=auth_headers)
    including = await client.get("/customers/lookup?excludeActivated=false", headers=auth_headers)

    ids = [row["customerId"] for row in default.json()["data"]]
    assert ids == [plain.customer_id]
    assert default.json()["data"][0]["label"] == f"{plain.customer_id} - Susanto"
    assert len(including.json()["data"]) == 2


async def test_lookup_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/customers/lookup")).status_code == 401
