"""Conformance with docs/openapi.yaml, which is the contract of record.

FastAPI generates its own /openapi.json. That is a convenience for Swagger UI, not the
contract. These tests check the code against the hand-written document, so a drift shows
up as a failing test rather than as a client that breaks in the field.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from httpx import AsyncClient

from app.main import app

CONTRACT = yaml.safe_load(Path("docs/openapi.yaml").read_text(encoding="utf-8"))


def _generated_paths() -> set[str]:
    # The contract's servers already carry the /v1 prefix, so strip it before comparing.
    return {path.removeprefix("/v1") for path in app.openapi()["paths"]}


def test_the_contract_is_a_valid_openapi_document() -> None:
    from openapi_spec_validator import validate

    validate(CONTRACT)


def test_every_declared_path_is_implemented() -> None:
    missing = set(CONTRACT["paths"]) - _generated_paths()

    assert not missing, f"declared in the contract but not implemented: {sorted(missing)}"


def test_no_endpoint_exists_that_the_contract_does_not_declare() -> None:
    """An endpoint the reviewers never saw is a contract change, not a feature."""
    extra = _generated_paths() - set(CONTRACT["paths"])

    assert not extra, f"implemented but absent from the contract: {sorted(extra)}"


def _operations_naming_an_ae(spec: dict, *, resolve: bool = False) -> list[tuple[str, str, dict]]:
    """Every operation that takes an `aeId`, whatever kind of parameter it is.

    `resolve` follows `$ref` parameters, which the hand-written contract uses and the
    generated one does not.
    """
    found = []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            for parameter in operation.get("parameters", []):
                if resolve and "$ref" in parameter:
                    name = parameter["$ref"].rsplit("/", 1)[-1]
                    parameter = spec["components"]["parameters"].get(name, {})
                if parameter.get("name", "").lower() in {"aeid", "ae_id"}:
                    found.append((method.upper(), path, operation))
    return found


def test_no_ae_audience_endpoint_accepts_an_ae_id() -> None:
    """Golden rule 2, unchanged: a mobile client must never write on another AE's behalf.

    Narrowed from "no endpoint" to "no AE-audience endpoint" when the supply chain
    module landed, and the distinction is the whole point of the second token audience
    rather than a loosening of the rule. The dashboard exists to let an MPX act on
    named salesmen — `GET /admin/account-executives/{aeId}` and `POST /admin/allocations`
    cannot work otherwise — so admin routes are scoped by the organisation binding on
    the token instead. On the AE audience nothing changed: identity still comes from the
    JWT and from nowhere else.
    """
    # Checked against the generated spec, so this catches an implementation that grows
    # a parameter the contract never declared.
    offenders = [
        f"{method} {path}"
        for method, path, _ in _operations_naming_an_ae(app.openapi())
        if "/admin/" not in path
    ]

    assert not offenders, f"aeId must come from the JWT on AE routes: {offenders}"


def test_every_endpoint_naming_an_ae_is_admin_audience() -> None:
    """The other half of the rule above, and the one that keeps it honest.

    Scoping the previous test to `/admin` would be worth nothing if an operation could
    name an `aeId` while being reachable with an AE token. Every such operation must
    demand `adminBearerAuth`, so the path prefix is never the thing doing the work.
    """
    # Checked against the contract, not the generated spec: FastAPI mints one
    # HTTPBearer scheme for both audiences and cannot tell them apart, while
    # docs/openapi.yaml — the document of record — names adminBearerAuth explicitly.
    offenders = []
    for method, path, operation in _operations_naming_an_ae(CONTRACT, resolve=True):
        schemes = {name for entry in operation.get("security", []) for name in entry}
        if "adminBearerAuth" not in schemes:
            offenders.append(f"{method} {path} (security={sorted(schemes) or 'inherited'})")

    assert not offenders, f"an aeId is only safe behind the admin audience: {offenders}"


@pytest.mark.parametrize(
    "status_enum",
    [
        (["EDUKASI", "HOT_LEADS", "PURCHASE"], "CustomerStatus"),
        (["NOT_ACTIVATED", "ACTIVATED", "FAILED", "CANCELLED"], "ActivationStatus"),
    ],
)
def test_enum_values_match_the_contract(status_enum) -> None:
    expected, name = status_enum

    assert sorted(CONTRACT["components"]["schemas"][name]["enum"]) == sorted(expected)


def test_the_rejected_taxonomy_appears_nowhere_in_the_generated_schema() -> None:
    """Potential / Sell In / Non-Potential is the Solution Description's vocabulary for
    the same concept. The i-Sales taxonomy is authoritative and the other must not leak
    into the API."""
    generated = str(app.openapi())

    for term in ("NON_POTENTIAL", 'SELL_IN"', "'SELL_IN'"):
        assert term not in generated


async def test_a_field_the_contract_types_as_nullable_keeps_its_null(
    client: AsyncClient, auth_headers, ae, make_inventory, make_customer
) -> None:
    """`activationDate` is declared oneOf [date-time, null]. The client reads null as
    "submitted, not yet live" — the red badge — so the key must survive."""
    item = await make_inventory(ae=ae)
    customer = await make_customer(ae=ae)

    created = await client.post(
        "/activations",
        json={
            "customerId": customer.customer_id,
            "msisdn": item.msisdn,
            "latitude": -6.2,
            "longitude": 106.9,
        },
        headers=auth_headers,
    )

    assert "activationDate" in created.json()
    assert created.json()["activationDate"] is None


async def test_a_field_the_contract_types_as_plain_is_omitted_when_absent(
    client: AsyncClient, auth_headers, ae, make_customer
) -> None:
    """`accuracyM` is declared {type: number} with no null union, so a customer
    registered without an accuracy reading omits the key rather than sending null."""
    response = await client.post(
        "/customers",
        json={
            "visitDate": "2026-08-19",
            "fullName": "Tanpa Akurasi",
            "phoneNumber": "081399887766",
            "address": "Jl. Tanpa Nama",
            "latitude": -6.2,
            "longitude": 106.9,
            "status": "EDUKASI",
        },
        headers=auth_headers,
    )

    assert response.status_code == 201
    assert "accuracyM" not in response.json()["geo"]


async def test_attendance_today_still_reports_an_explicit_null(
    client: AsyncClient, auth_headers
) -> None:
    """Documented as "`null` data means not yet checked in" — an empty object would not
    say the same thing."""
    response = await client.get("/attendance/today", headers=auth_headers)

    assert response.json() == {"data": None}


async def test_every_error_response_uses_the_single_envelope(
    client: AsyncClient, auth_headers, ae, make_customer
) -> None:
    theirs = await client.get("/customers/999999999", headers=auth_headers)
    unauthorised = await client.get("/me")
    invalid = await client.post("/customers", json={}, headers=auth_headers)

    for response in (theirs, unauthorised, invalid):
        body = response.json()
        assert set(body) == {"error"}
        assert isinstance(body["error"]["code"], str)
        assert body["error"]["code"].isupper()
        assert isinstance(body["error"]["message"], str)


async def test_error_messages_carry_no_pii(
    client: AsyncClient, auth_headers, ae, make_customer
) -> None:
    """This system holds names, phone numbers, addresses and GPS coordinates. None of
    it belongs in a message that gets logged by whoever receives it."""
    customer = await make_customer(ae=ae)
    duplicate = await client.post(
        "/customers",
        json={
            "visitDate": "2026-08-19",
            "fullName": customer.full_name,
            "phoneNumber": customer.phone_number,
            "address": customer.address,
            "latitude": -6.2,
            "longitude": 106.9,
            "status": "PURCHASE",
        },
        headers=auth_headers,
    )

    assert duplicate.status_code == 409
    message = duplicate.json()["error"]["message"]
    assert customer.phone_number not in message
    assert customer.full_name not in message
    assert customer.address not in message


async def test_a_404_never_becomes_a_403_for_another_aes_record(
    client: AsyncClient, auth_headers, other_ae, make_customer
) -> None:
    theirs = await make_customer(ae=other_ae)

    existing_but_not_mine = await client.get(
        f"/customers/{theirs.customer_id}", headers=auth_headers
    )
    does_not_exist = await client.get("/customers/1000000000000", headers=auth_headers)

    assert existing_but_not_mine.status_code == does_not_exist.status_code == 404
    assert existing_but_not_mine.json() == does_not_exist.json()
