"""Steps 5-7 end to end: DP requests, IOH supplies, DP pairs, MPX orders, DP attaches.

Written as one narrative walk plus the negative cases, because the value of this module
is that the handoffs work — each use case in isolation proves much less than the chain
does. Where a test asserts money or counts, those are the numbers three separate
companies will read off the same record.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.commercial import Address, Brand, CallPlan
from app.models.inventory import DeviceModel, FwaInventory

# ---------------------------------------------------------------------------
# Fixtures specific to the chain
# ---------------------------------------------------------------------------


@pytest.fixture
async def call_plan(session) -> CallPlan:
    return await session.scalar(select(CallPlan).where(CallPlan.code == "DATA_50GB"))


@pytest.fixture
async def brand(session) -> Brand:
    return await session.scalar(select(Brand).where(Brand.code == "IM3"))


@pytest.fixture
async def priced_model(session, device_partner) -> DeviceModel:
    """The catalogue ships unpriced, so an orderable model has to be given a price."""
    model = DeviceModel(
        model_code=f"ADVAN V1 PRO {uuid.uuid4().hex[:4]}",
        brand="ADVAN",
        device_partner_id=device_partner.device_partner_id,
        list_price_idr=390_000,
    )
    session.add(model)
    await session.flush()
    return model


@pytest.fixture
async def mpx_address(session, mpx) -> Address:
    address = Address(
        mpx_id=mpx.mpx_id,
        label="Gudang Utama",
        recipient_name="Budi Santoso",
        recipient_phone="081234567890",
        line1="Jl. Mawar No. 1",
        city="Bengkulu",
        province="Bengkulu",
    )
    session.add(address)
    await session.flush()
    return address


def numbers(count: int, start: int = 6289900000000) -> list[str]:
    return [str(start + i) for i in range(count)]


def imeis(count: int, start: int = 350000000000000) -> list[str]:
    return [str(start + i) for i in range(count)]


# ---------------------------------------------------------------------------
# The happy path, end to end
# ---------------------------------------------------------------------------


async def test_the_whole_chain_from_request_to_attached_bundles(
    client: AsyncClient,
    session,
    admin_login,
    dp_admin,
    ioh_admin,
    mpx_admin,
    call_plan,
    brand,
    priced_model,
    mpx_address,
) -> None:
    dp = await admin_login(dp_admin)
    ioh = await admin_login(ioh_admin)
    mpx_hdr = await admin_login(mpx_admin)

    # 1. DP asks IOH for 10 numbers.
    created = await client.post(
        "/admin/msisdn-pos",
        json={
            "callPlanId": str(call_plan.call_plan_id),
            "brandCode": brand.code,
            "qtyRequested": 10,
        },
        headers=dp,
    )
    assert created.status_code == 201, created.text
    po = created.json()
    po_id = po["msisdnPoId"]
    assert po["status"] == "DIAJUKAN"
    # {DP_CODE}-{YYYYMMDD}-{SEQ}, generated server-side.
    assert po["poCode"].startswith("ADVAN-")
    assert po["qtySupplied"] == 0

    # 2. IOH picks it up, then supplies exactly ten numbers.
    picked = await client.post(f"/admin/msisdn-pos/{po_id}/process", headers=ioh)
    assert picked.status_code == 200, picked.text
    assert picked.json()["status"] == "DIPROSES"

    msisdns = numbers(10)
    supplied = await client.post(
        f"/admin/msisdn-pos/{po_id}/supply", json={"msisdns": msisdns}, headers=ioh
    )
    assert supplied.status_code == 200, supplied.text
    assert supplied.json() == {"msisdnPoId": po_id, "status": "DITERIMA", "supplied": 10}

    # The supplied numbers ARE inventory rows — there is no separate item table.
    rows = (
        await session.scalars(
            select(FwaInventory).where(FwaInventory.msisdn_po_id == uuid.UUID(po_id))
        )
    ).all()
    assert len(rows) == 10
    assert all(row.imei is None for row in rows)
    assert all(row.status.value == "MSISDN_ISSUED" for row in rows)

    # 3. DP pairs an IMEI to each.
    paired = await client.post(
        f"/admin/msisdn-pos/{po_id}/pairing",
        json={"pairs": [{"msisdn": m, "imei": i} for m, i in zip(msisdns, imeis(10), strict=True)]},
        headers=dp,
    )
    assert paired.status_code == 200, paired.text
    assert paired.json()["paired"] == 10

    # 4. MPX orders 10 devices from that DP.
    ordered = await client.post(
        "/admin/device-pos",
        json={
            "devicePartnerId": str(dp_admin.device_partner_id),
            "deviceModelId": str(priced_model.device_model_id),
            "brandCode": brand.code,
            "qty": 10,
            "addressId": str(mpx_address.address_id),
        },
        headers=mpx_hdr,
    )
    assert ordered.status_code == 201, ordered.text
    device_po = ordered.json()
    device_po_id = device_po["devicePoId"]
    assert device_po["status"] == "DIAJUKAN"
    # Money is whole rupiah, and the total is enforced by the database.
    assert device_po["unitPriceIdr"] == 390_000
    assert device_po["totalIdr"] == 3_900_000

    # 5. DP accepts and attaches the ten bundles it just paired.
    accepted = await client.post(f"/admin/device-pos/{device_po_id}/accept", headers=dp)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "DIPROSES"

    attached = await client.post(
        f"/admin/device-pos/{device_po_id}/bundles", json={"msisdns": msisdns}, headers=dp
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["attached"] == 10

    # The units now know which order and which MPX they belong to.
    await session.refresh(rows[0])
    assert rows[0].device_po_id == uuid.UUID(device_po_id)
    assert rows[0].mpx_id == mpx_admin.mpx_id
    assert rows[0].status.value == "ASSIGNED"

    # 6. The Riwayat panel is a true record, because every transition wrote to it.
    detail = await client.get(f"/admin/device-pos/{device_po_id}", headers=mpx_hdr)
    assert detail.status_code == 200, detail.text
    history = detail.json()["statusHistory"]
    assert [row["newStatus"] for row in history][:2] == ["DIAJUKAN", "DIPROSES"]
    assert all(row["changedBy"] for row in history)
    assert detail.json()["address"]["recipientName"] == "Budi Santoso"

    # And the AE app still cannot see any of it — nothing is ALLOCATED yet.
    assert all(row.allocated_ae_id is None for row in rows)


# ---------------------------------------------------------------------------
# Quantity conservation — golden rule 9
# ---------------------------------------------------------------------------


@pytest.fixture
async def supplied_po(client, admin_login, dp_admin, ioh_admin, call_plan, brand):
    """A PO with ten numbers issued and no IMEIs yet."""
    dp = await admin_login(dp_admin)
    ioh = await admin_login(ioh_admin)
    created = await client.post(
        "/admin/msisdn-pos",
        json={
            "callPlanId": str(call_plan.call_plan_id),
            "brandCode": brand.code,
            "qtyRequested": 10,
        },
        headers=dp,
    )
    po_id = created.json()["msisdnPoId"]
    msisdns = numbers(10, 6289911100000)
    await client.post(f"/admin/msisdn-pos/{po_id}/supply", json={"msisdns": msisdns}, headers=ioh)
    return po_id, msisdns, dp, ioh


@pytest.mark.parametrize("count", [9, 11])
async def test_supplying_the_wrong_number_of_msisdns_is_rejected_entirely(
    client, admin_login, dp_admin, ioh_admin, call_plan, brand, session, count
) -> None:
    """A partial import is worse than a failed one: nothing tells you which half landed."""
    dp = await admin_login(dp_admin)
    ioh = await admin_login(ioh_admin)
    created = await client.post(
        "/admin/msisdn-pos",
        json={
            "callPlanId": str(call_plan.call_plan_id),
            "brandCode": brand.code,
            "qtyRequested": 10,
        },
        headers=dp,
    )
    po_id = created.json()["msisdnPoId"]

    response = await client.post(
        f"/admin/msisdn-pos/{po_id}/supply",
        json={"msisdns": numbers(count, 6289922200000)},
        headers=ioh,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "QTY_MISMATCH"
    written = await session.scalar(
        select(FwaInventory).where(FwaInventory.msisdn_po_id == uuid.UUID(po_id))
    )
    assert written is None


async def test_pairing_the_wrong_number_of_imeis_is_rejected_entirely(client, supplied_po) -> None:
    po_id, msisdns, dp, _ = supplied_po

    response = await client.post(
        f"/admin/msisdn-pos/{po_id}/pairing",
        json={
            "pairs": [
                {"msisdn": m, "imei": i}
                for m, i in zip(msisdns[:9], imeis(9, 350000000009000), strict=True)
            ]
        },
        headers=dp,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "IMEI_COUNT_MISMATCH"


async def test_an_imei_cannot_be_paired_to_two_numbers(client, supplied_po) -> None:
    """IMEIs are unique platform-wide: a duplicate is a device that exists twice."""
    po_id, msisdns, dp, _ = supplied_po
    duplicated = [*imeis(9, 350000000020000), str(350000000020000)]

    response = await client.post(
        f"/admin/msisdn-pos/{po_id}/pairing",
        json={
            "pairs": [{"msisdn": m, "imei": i} for m, i in zip(msisdns, duplicated, strict=True)]
        },
        headers=dp,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "PAIRING_REJECTED"


async def test_msisdns_are_normalised_before_validation(
    client, admin_login, dp_admin, ioh_admin, call_plan, brand, session
) -> None:
    """Golden rule 11. A spreadsheet column of 08… numbers must not be rejected by the
    ^62… CHECK with an error the operator cannot act on."""
    dp = await admin_login(dp_admin)
    ioh = await admin_login(ioh_admin)
    created = await client.post(
        "/admin/msisdn-pos",
        json={
            "callPlanId": str(call_plan.call_plan_id),
            "brandCode": brand.code,
            "qtyRequested": 2,
        },
        headers=dp,
    )
    po_id = created.json()["msisdnPoId"]

    response = await client.post(
        f"/admin/msisdn-pos/{po_id}/supply",
        json={"msisdns": ["089933300001", "+62 899-3330-0002"]},
        headers=ioh,
    )

    assert response.status_code == 200, response.text
    stored = (
        await session.scalars(
            select(FwaInventory.msisdn).where(FwaInventory.msisdn_po_id == uuid.UUID(po_id))
        )
    ).all()
    assert sorted(stored) == ["6289933300001", "6289933300002"]


# ---------------------------------------------------------------------------
# Replay safety — golden rule 10
# ---------------------------------------------------------------------------


async def test_a_retried_supply_inserts_nothing_the_second_time(
    client, admin_login, dp_admin, ioh_admin, call_plan, brand, session
) -> None:
    dp = await admin_login(dp_admin)
    ioh = await admin_login(ioh_admin)
    created = await client.post(
        "/admin/msisdn-pos",
        json={
            "callPlanId": str(call_plan.call_plan_id),
            "brandCode": brand.code,
            "qtyRequested": 5,
        },
        headers=dp,
    )
    po_id = created.json()["msisdnPoId"]
    key = str(uuid.uuid4())
    body = {"msisdns": numbers(5, 6289944400000)}

    first = await client.post(
        f"/admin/msisdn-pos/{po_id}/supply",
        json=body,
        headers={**ioh, "Idempotency-Key": key},
    )
    second = await client.post(
        f"/admin/msisdn-pos/{po_id}/supply",
        json=body,
        headers={**ioh, "Idempotency-Key": key},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()

    total = len(
        (
            await session.scalars(
                select(FwaInventory).where(FwaInventory.msisdn_po_id == uuid.UUID(po_id))
            )
        ).all()
    )
    assert total == 5


async def test_the_same_key_with_a_different_payload_is_409(
    client, admin_login, dp_admin, ioh_admin, call_plan, brand
) -> None:
    dp = await admin_login(dp_admin)
    ioh = await admin_login(ioh_admin)
    created = await client.post(
        "/admin/msisdn-pos",
        json={
            "callPlanId": str(call_plan.call_plan_id),
            "brandCode": brand.code,
            "qtyRequested": 3,
        },
        headers=dp,
    )
    po_id = created.json()["msisdnPoId"]
    key = str(uuid.uuid4())

    await client.post(
        f"/admin/msisdn-pos/{po_id}/supply",
        json={"msisdns": numbers(3, 6289955500000)},
        headers={**ioh, "Idempotency-Key": key},
    )
    response = await client.post(
        f"/admin/msisdn-pos/{po_id}/supply",
        json={"msisdns": numbers(3, 6289955600000)},
        headers={**ioh, "Idempotency-Key": key},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


# ---------------------------------------------------------------------------
# Org scoping — golden rule 7
# ---------------------------------------------------------------------------


async def test_a_device_partner_cannot_see_another_partners_po(
    client, session, admin_login, dp_admin, make_admin, call_plan, brand
) -> None:
    """404, not 403. A 403 would confirm the order exists to a competitor."""
    from app.models.organisation import DevicePartner

    dp = await admin_login(dp_admin)
    created = await client.post(
        "/admin/msisdn-pos",
        json={
            "callPlanId": str(call_plan.call_plan_id),
            "brandCode": brand.code,
            "qtyRequested": 4,
        },
        headers=dp,
    )
    po_id = created.json()["msisdnPoId"]

    rival_partner = await session.scalar(select(DevicePartner).where(DevicePartner.code == "RABIT"))
    rival_admin = await make_admin(device_partner=rival_partner)
    rival = await admin_login(rival_admin)

    listed = await client.get("/admin/msisdn-pos", headers=rival)
    fetched = await client.get(f"/admin/msisdn-pos/{po_id}", headers=rival)

    assert listed.json()["meta"]["total"] == 0
    assert fetched.status_code == 404
    assert fetched.json()["error"]["code"] == "PO_NOT_FOUND"


async def test_ioh_sees_every_partners_requests(
    client, admin_login, dp_admin, ioh_admin, call_plan, brand
) -> None:
    """IOH is global-read by design — it is the counterparty to all of them."""
    dp = await admin_login(dp_admin)
    ioh = await admin_login(ioh_admin)
    await client.post(
        "/admin/msisdn-pos",
        json={
            "callPlanId": str(call_plan.call_plan_id),
            "brandCode": brand.code,
            "qtyRequested": 6,
        },
        headers=dp,
    )

    listed = await client.get("/admin/msisdn-pos", headers=ioh)

    assert listed.status_code == 200, listed.text
    assert listed.json()["meta"]["total"] >= 1


# ---------------------------------------------------------------------------
# Transitions — golden rule 8
# ---------------------------------------------------------------------------


async def test_a_supplied_po_cannot_be_cancelled(client, supplied_po) -> None:
    po_id, _, dp, _ = supplied_po

    response = await client.post(f"/admin/msisdn-pos/{po_id}/cancel", headers=dp)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_TRANSITION"


async def test_rejecting_requires_a_reason_and_records_it(
    client, admin_login, dp_admin, ioh_admin, call_plan, brand
) -> None:
    """Confirmed 2026-09-01: IOH has discretion to refuse, and a refusal the DP cannot
    act on is not a refusal."""
    dp = await admin_login(dp_admin)
    ioh = await admin_login(ioh_admin)
    created = await client.post(
        "/admin/msisdn-pos",
        json={
            "callPlanId": str(call_plan.call_plan_id),
            "brandCode": brand.code,
            "qtyRequested": 7,
        },
        headers=dp,
    )
    po_id = created.json()["msisdnPoId"]

    without = await client.post(f"/admin/msisdn-pos/{po_id}/reject", json={}, headers=ioh)
    assert without.status_code == 422

    rejected = await client.post(
        f"/admin/msisdn-pos/{po_id}/reject",
        json={"reason": "Kuota nomor untuk call plan ini habis."},
        headers=ioh,
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "DITOLAK"
    assert rejected.json()["rejectedReason"] == "Kuota nomor untuk call plan ini habis."

    # And the DP can see why, on their own list.
    detail = await client.get(f"/admin/msisdn-pos/{po_id}", headers=dp)
    assert detail.json()["rejectedReason"] == "Kuota nomor untuk call plan ini habis."
    assert detail.json()["statusHistory"][-1]["newStatus"] == "DITOLAK"


async def test_a_dp_cannot_supply_its_own_request(client, supplied_po, admin_login, dp_admin):
    """The whole point of two organisations is that one cannot do the other's step."""
    po_id, _, dp, _ = supplied_po

    response = await client.post(
        f"/admin/msisdn-pos/{po_id}/supply", json={"msisdns": numbers(10)}, headers=dp
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_NOT_PERMITTED"


# ---------------------------------------------------------------------------
# Device PO specifics
# ---------------------------------------------------------------------------


async def test_an_unpriced_model_cannot_be_ordered(
    client, session, admin_login, mpx_admin, dp_admin, brand, mpx_address, device_partner
) -> None:
    """No confirmed price list exists yet. A guessed price on a purchase order that
    three companies read is worse than a blocked form."""
    unpriced = DeviceModel(
        model_code=f"UNPRICED {uuid.uuid4().hex[:4]}",
        brand="ADVAN",
        device_partner_id=device_partner.device_partner_id,
    )
    session.add(unpriced)
    await session.flush()
    mpx_hdr = await admin_login(mpx_admin)

    response = await client.post(
        "/admin/device-pos",
        json={
            "devicePartnerId": str(dp_admin.device_partner_id),
            "deviceModelId": str(unpriced.device_model_id),
            "brandCode": brand.code,
            "qty": 5,
            "addressId": str(mpx_address.address_id),
        },
        headers=mpx_hdr,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "MODEL_NOT_PRICED"


async def test_an_mpx_cannot_deliver_to_another_mpxs_address(
    client, session, admin_login, mpx_admin, dp_admin, brand, priced_model
) -> None:
    from app.models.organisation import Mpx

    other_mpx = await session.scalar(select(Mpx).where(Mpx.code == "MPX-SDA-02"))
    foreign = Address(
        mpx_id=other_mpx.mpx_id,
        label="Bukan Punya Saya",
        recipient_name="Orang Lain",
        recipient_phone="081200000000",
        line1="Jl. Lain",
        city="Sidoarjo",
        province="Jawa Timur",
    )
    session.add(foreign)
    await session.flush()
    mpx_hdr = await admin_login(mpx_admin)

    response = await client.post(
        "/admin/device-pos",
        json={
            "devicePartnerId": str(dp_admin.device_partner_id),
            "deviceModelId": str(priced_model.device_model_id),
            "brandCode": brand.code,
            "qty": 5,
            "addressId": str(foreign.address_id),
        },
        headers=mpx_hdr,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ADDRESS_NOT_FOUND"
