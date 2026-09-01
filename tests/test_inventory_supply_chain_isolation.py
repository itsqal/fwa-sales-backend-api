"""Migration 0005 must not make supply-chain stock visible to the mobile app.

This is the regression suite for the one migration in the programme that could take the
field force offline. `fwa_inventory` now holds units belonging to the supply chain
rather than to any salesman, and one of those states carries `imei IS NULL` — which the
AE contract types as a required string.

Every test here is written from the mobile app's point of view: as far as an AE is
concerned, a unit that has not been allocated to somebody does not exist.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

from app.models.enums import InventoryStatus

# Every state a unit passes through before an MPX admin hands it to a salesman.
SUPPLY_CHAIN_STATUSES = [
    InventoryStatus.MSISDN_ISSUED,
    InventoryStatus.PAIRED,
    InventoryStatus.ASSIGNED,
    InventoryStatus.SHIPPED,
    InventoryStatus.RECEIVED,
]


# ---------------------------------------------------------------------------
# The barcode scanner — the one AE read with no ownership filter
# ---------------------------------------------------------------------------


async def test_an_unpaired_number_is_a_404_not_a_500(
    client: AsyncClient, auth_headers: dict[str, str], make_inventory
) -> None:
    """The failure this whole migration was designed around.

    `lookup_msisdn` fetches by MSISDN with no `allocated_ae_id` filter, so without the
    status guard it would resolve a number IOH had just supplied and then try to
    serialise `imei=None` into a required string — a 500 on the scanner, for a number
    an AE has every reason to scan off a box.
    """
    unit = await make_inventory(imei=None, status=InventoryStatus.MSISDN_ISSUED)

    response = await client.get(f"/inventory/msisdn/{unit.msisdn}", headers=auth_headers)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "MSISDN_NOT_FOUND"


@pytest.mark.parametrize("status", SUPPLY_CHAIN_STATUSES)
async def test_no_supply_chain_state_resolves_for_an_ae(
    client: AsyncClient, auth_headers: dict[str, str], make_inventory, status: InventoryStatus
) -> None:
    """Not only the unpaired one. A unit sitting in an MPX warehouse is real, paired,
    and still none of a salesman's business until it is allocated to them."""
    imei = None if status is InventoryStatus.MSISDN_ISSUED else ...
    unit = await make_inventory(status=status, **({"imei": None} if imei is None else {}))

    response = await client.get(f"/inventory/msisdn/{unit.msisdn}", headers=auth_headers)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "MSISDN_NOT_FOUND"


async def test_an_allocated_number_still_resolves_normally(
    client: AsyncClient, auth_headers: dict[str, str], ae, make_inventory
) -> None:
    """The regression guard. The filter must not break the flow it protects."""
    unit = await make_inventory(ae=ae, status=InventoryStatus.ALLOCATED)

    response = await client.get(f"/inventory/msisdn/{unit.msisdn}", headers=auth_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["msisdn"] == unit.msisdn
    assert body["imei"] == unit.imei
    assert body["eligible"] is True


# ---------------------------------------------------------------------------
# The stock lists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", SUPPLY_CHAIN_STATUSES)
async def test_supply_chain_stock_never_appears_in_my_inventory(
    client: AsyncClient, auth_headers: dict[str, str], ae, make_inventory, status: InventoryStatus
) -> None:
    """Defence in depth.

    `allocated_ae_id` is NULL on supply-chain rows, so this list already excludes them.
    But nothing in the database enforces that pairing, so the unit is deliberately given
    an owner here — a state that should be impossible — to prove the status filter and
    not just the ownership filter is doing the work.
    """
    await make_inventory(
        ae=ae,
        status=status,
        **({"imei": None} if status is InventoryStatus.MSISDN_ISSUED else {}),
    )

    response = await client.get("/inventory/me", headers=auth_headers)

    assert response.status_code == 200, response.text
    assert response.json()["meta"]["total"] == 0


async def test_my_inventory_still_returns_allocated_stock(
    client: AsyncClient, auth_headers: dict[str, str], ae, make_inventory
) -> None:
    await make_inventory(ae=ae, status=InventoryStatus.ALLOCATED)
    await make_inventory(ae=ae, status=InventoryStatus.ALLOCATED)

    response = await client.get("/inventory/me", headers=auth_headers)

    assert response.json()["meta"]["total"] == 2


async def test_the_offline_bootstrap_carries_no_supply_chain_stock(
    client: AsyncClient, auth_headers: dict[str, str], ae, make_inventory
) -> None:
    """`/sync/bootstrap` is the third read path, and the one the spec did not name."""
    await make_inventory(ae=ae, status=InventoryStatus.ALLOCATED)
    await make_inventory(ae=ae, status=InventoryStatus.RECEIVED)
    await make_inventory(ae=ae, imei=None, status=InventoryStatus.MSISDN_ISSUED)

    response = await client.get("/sync/bootstrap", headers=auth_headers)

    assert response.status_code == 200, response.text
    stock = response.json()["inventory"]
    assert len(stock) == 1
    # Every item the device caches offline must carry an IMEI: the activation form
    # renders it read-only, and there is no network to go back and ask.
    assert all(item["imei"] for item in stock)


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


async def test_an_unpaired_number_cannot_be_activated(
    client: AsyncClient, auth_headers: dict[str, str], ae, make_customer, make_inventory
) -> None:
    """`activation.imei` is NOT NULL and is snapshotted from inventory. Eligibility is
    assessed before that read, so this fails cleanly rather than as an IntegrityError."""
    customer = await make_customer(ae=ae)
    unit = await make_inventory(imei=None, status=InventoryStatus.MSISDN_ISSUED)

    response = await client.post(
        "/activations",
        json={
            "customerId": customer.customer_id,
            "msisdn": unit.msisdn,
            "latitude": -3.79,
            "longitude": 102.26,
        },
        headers=auth_headers,
    )

    assert response.status_code in {404, 422}, response.text
    assert response.status_code != 500
    assert response.json()["error"]["code"] != "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# The database guarantee behind all of the above
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        InventoryStatus.ALLOCATED,
        InventoryStatus.ACTIVATED,
        InventoryStatus.RECEIVED,
        InventoryStatus.PAIRED,
    ],
)
async def test_a_null_imei_cannot_escape_the_pre_pairing_state(
    session, make_inventory, status: InventoryStatus
) -> None:
    """`imei_required_once_paired` is what makes the read filters sufficient rather
    than merely likely: a NULL IMEI simply cannot reach a status the app renders."""
    with pytest.raises(IntegrityError):
        await make_inventory(imei=None, status=status)
    await session.rollback()


async def test_a_unit_the_ae_just_sold_stays_visible(
    client: AsyncClient, auth_headers: dict[str, str], ae, make_customer, make_inventory
) -> None:
    """CONSUMED must stay inside AE_VISIBLE_STATUSES.

    The Supply Chain spec describes CONSUMED as "now unused" and prescribes a filter
    that omits it. That is wrong: the activation path writes CONSUMED on every unit an
    AE sells. Filtering it out would make a box vanish the moment it was activated, so
    rescanning it would answer "not a registered HiFi AIR unit" instead of "already
    activated" — and the duplicate-activation 409 would be unreachable.
    """
    unit = await make_inventory(ae=ae, status=InventoryStatus.ALLOCATED)
    customer = await make_customer(ae=ae)

    activated = await client.post(
        "/activations",
        json={
            "customerId": customer.customer_id,
            "msisdn": unit.msisdn,
            "latitude": -3.79,
            "longitude": 102.26,
        },
        headers=auth_headers,
    )
    assert activated.status_code == 201, activated.text

    rescanned = await client.get(f"/inventory/msisdn/{unit.msisdn}", headers=auth_headers)

    assert rescanned.status_code == 200, rescanned.text
    body = rescanned.json()
    assert body["status"] == "CONSUMED"
    assert body["eligible"] is False
    assert body["reason"] == "ALREADY_ACTIVATED"
