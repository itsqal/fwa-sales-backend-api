"""The acceptance test for the whole programme.

Supply Chain spec §7: *"Step 9 is the acceptance test worth writing first... DP requests
10 → IOH supplies 10 → DP pairs 10 → MPX orders 10 → DP attaches and ships → MPX
receives → MPX allocates to AE → `GET /inventory/me` as that AE returns 10 items and
`GET /inventory/msisdn/{one of them}` returns `eligible: true`."*

Until this passes, the two halves of the programme are separate products. It is written
as one long test on purpose: the value is in the handoffs, and splitting it into nine
would test nine things that already have their own tests while testing the one thing
that matters — that they join up — not at all.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.commercial import Address, Brand, CallPlan
from app.models.inventory import DeviceModel


@pytest.fixture
async def call_plan(session) -> CallPlan:
    return await session.scalar(select(CallPlan).where(CallPlan.code == "DATA_50GB"))


@pytest.fixture
async def brand(session) -> Brand:
    return await session.scalar(select(Brand).where(Brand.code == "IM3"))


@pytest.fixture
async def priced_model(session, device_partner) -> DeviceModel:
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
        is_default=True,
    )
    session.add(address)
    await session.flush()
    return address


async def test_a_unit_travels_from_an_ioh_number_to_a_salesmans_phone(
    client: AsyncClient,
    session,
    admin_login,
    dp_admin,
    ioh_admin,
    mpx_admin,
    ae,
    call_plan,
    brand,
    priced_model,
    mpx_address,
) -> None:
    dp = await admin_login(dp_admin)
    ioh = await admin_login(ioh_admin)
    mpx_hdr = await admin_login(mpx_admin)
    msisdns = [str(6289700000000 + i) for i in range(10)]

    # --- 1. DP requests 10 numbers -----------------------------------------
    po = (
        await client.post(
            "/admin/msisdn-pos",
            json={
                "callPlanId": str(call_plan.call_plan_id),
                "brandCode": brand.code,
                "qtyRequested": 10,
            },
            headers=dp,
        )
    ).json()
    msisdn_po_id = po["msisdnPoId"]

    # --- 2. IOH supplies them ----------------------------------------------
    supplied = await client.post(
        f"/admin/msisdn-pos/{msisdn_po_id}/supply", json={"msisdns": msisdns}, headers=ioh
    )
    assert supplied.status_code == 200, supplied.text
    assert supplied.json()["status"] == "DITERIMA"

    # --- 3. DP pairs an IMEI to each ---------------------------------------
    paired = await client.post(
        f"/admin/msisdn-pos/{msisdn_po_id}/pairing",
        json={
            "pairs": [
                {"msisdn": m, "imei": str(357700000000000 + i)} for i, m in enumerate(msisdns)
            ]
        },
        headers=dp,
    )
    assert paired.status_code == 200, paired.text

    # --- 4. MPX orders 10 devices ------------------------------------------
    device_po = (
        await client.post(
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
    ).json()
    device_po_id = device_po["devicePoId"]
    assert device_po["totalIdr"] == 3_900_000

    # --- 5. DP accepts and attaches ----------------------------------------
    await client.post(f"/admin/device-pos/{device_po_id}/accept", headers=dp)
    attached = await client.post(
        f"/admin/device-pos/{device_po_id}/bundles", json={"msisdns": msisdns}, headers=dp
    )
    assert attached.status_code == 200, attached.text

    # --- 6. DP ships with a courier and AWB --------------------------------
    shipped = await client.post(
        f"/admin/device-pos/{device_po_id}/shipment",
        json={"courierName": "J&T Express", "awb": "JD0463672772"},
        headers=dp,
    )
    assert shipped.status_code == 200, shipped.text
    assert [m["milestone"] for m in shipped.json()["milestones"]] == ["SHIPPED"]

    # The Riwayat panel carries the courier and resi, exactly as the mockup shows.
    detail = (await client.get(f"/admin/device-pos/{device_po_id}", headers=mpx_hdr)).json()
    assert detail["status"] == "DIKIRIM"
    # .get, not ["note"]: a history row with no keterangan omits the key entirely,
    # which is this codebase's absent-versus-null convention rather than an oversight.
    assert any(row.get("note") == "J&T Express | JD0463672772" for row in detail["statusHistory"])

    # --- 7. MPX inspects, then confirms the whole delivery ------------------
    inspected = await client.post(f"/admin/device-pos/{device_po_id}/inspect", headers=mpx_hdr)
    assert inspected.status_code == 200, inspected.text
    assert inspected.json()["data"] == "PERIKSA"

    received = await client.post(
        f"/admin/device-pos/{device_po_id}/receipt", json={"confirmed": True}, headers=mpx_hdr
    )
    assert received.status_code == 200, received.text
    assert received.json()["status"] == "DITERIMA"
    assert received.json()["qtyReceived"] == 10

    # --- 8. The stock now shows against this MPX ---------------------------
    stock = (await client.get("/admin/stock", headers=mpx_hdr)).json()["data"]
    line = next(row for row in stock if row["deviceModelCode"] == priced_model.model_code)
    assert (line["available"], line["allocated"], line["total"]) == (10, 0, 10)

    # Still invisible to the salesman: received is not the same as allocated.
    assert (await client.get("/inventory/me", headers=await _ae_headers(client, ae))).json()[
        "meta"
    ]["total"] == 0

    # --- 9. MPX allocates all ten to the AE --------------------------------
    allocated = await client.post(
        "/admin/allocations",
        json={
            "aeId": str(ae.ae_id),
            "mode": "AUTO",
            "items": [{"deviceModelId": str(priced_model.device_model_id), "qty": 10}],
        },
        headers=mpx_hdr,
    )
    assert allocated.status_code == 201, allocated.text
    assert allocated.json()["qty"] == 10
    assert sorted(allocated.json()["msisdns"]) == sorted(msisdns)

    after = (await client.get("/admin/stock", headers=mpx_hdr)).json()["data"]
    line = next(row for row in after if row["deviceModelCode"] == priced_model.model_code)
    assert (line["available"], line["allocated"], line["total"]) == (0, 10, 10)

    # --- 10. THE POINT: the AE mobile app can now see them -----------------
    ae_headers = await _ae_headers(client, ae)

    mine = await client.get("/inventory/me", headers=ae_headers)
    assert mine.status_code == 200, mine.text
    assert mine.json()["meta"]["total"] == 10

    scanned = await client.get(f"/inventory/msisdn/{msisdns[0]}", headers=ae_headers)
    assert scanned.status_code == 200, scanned.text
    body = scanned.json()
    assert body["eligible"] is True
    assert body["imei"] == "357700000000000"
    assert body["deviceModel"]["modelCode"] == priced_model.model_code

    # And the offline bootstrap carries them too.
    bootstrap = await client.get("/sync/bootstrap", headers=ae_headers)
    assert len(bootstrap.json()["inventory"]) == 10


async def _ae_headers(client: AsyncClient, ae) -> dict[str, str]:
    from tests.conftest import TEST_PASSWORD

    response = await client.post(
        "/auth/login", json={"aeCode": ae.ae_code, "password": TEST_PASSWORD}
    )
    return {"Authorization": f"Bearer {response.json()['accessToken']}"}


# ---------------------------------------------------------------------------
# The negative cases — "where money leaks", per the development plan
# ---------------------------------------------------------------------------


@pytest.fixture
async def received_stock(
    client,
    session,
    admin_login,
    dp_admin,
    ioh_admin,
    mpx_admin,
    call_plan,
    brand,
    priced_model,
    mpx_address,
):
    """Ten units received by the MPX and ready to allocate."""
    dp = await admin_login(dp_admin)
    ioh = await admin_login(ioh_admin)
    mpx_hdr = await admin_login(mpx_admin)
    msisdns = [str(6289600000000 + i) for i in range(10)]

    po = (
        await client.post(
            "/admin/msisdn-pos",
            json={
                "callPlanId": str(call_plan.call_plan_id),
                "brandCode": brand.code,
                "qtyRequested": 10,
            },
            headers=dp,
        )
    ).json()["msisdnPoId"]
    await client.post(f"/admin/msisdn-pos/{po}/supply", json={"msisdns": msisdns}, headers=ioh)
    await client.post(
        f"/admin/msisdn-pos/{po}/pairing",
        json={
            "pairs": [
                {"msisdn": m, "imei": str(357600000000000 + i)} for i, m in enumerate(msisdns)
            ]
        },
        headers=dp,
    )
    device_po = (
        await client.post(
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
    ).json()["devicePoId"]
    await client.post(f"/admin/device-pos/{device_po}/accept", headers=dp)
    await client.post(
        f"/admin/device-pos/{device_po}/bundles", json={"msisdns": msisdns}, headers=dp
    )
    await client.post(
        f"/admin/device-pos/{device_po}/shipment",
        json={"courierName": "J&T Express", "awb": "JD0000000001"},
        headers=dp,
    )
    await client.post(f"/admin/device-pos/{device_po}/inspect", headers=mpx_hdr)
    await client.post(
        f"/admin/device-pos/{device_po}/receipt", json={"confirmed": True}, headers=mpx_hdr
    )
    return device_po, msisdns, mpx_hdr, dp


async def test_a_short_delivery_cannot_be_confirmed(
    client,
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
    """Confirmed 2026-09-01: MPX confirms only when the full list has arrived.

    Simulated by one unit going astray after dispatch. The order stays at PERIKSA and
    nothing is written — the reshipping happens outside this system.
    """
    from app.models.enums import DevicePoStatus, InventoryStatus
    from app.models.fulfilment import GoodsReceipt
    from app.models.inventory import FwaInventory
    from app.models.purchasing import DevicePo

    device_po, msisdns, mpx_hdr, _dp = await _ship_ten(
        client,
        admin_login,
        dp_admin,
        ioh_admin,
        mpx_admin,
        call_plan,
        brand,
        priced_model,
        mpx_address,
        base=6289500000000,
        imei_base=357500000000000,
    )
    await client.post(f"/admin/device-pos/{device_po}/inspect", headers=mpx_hdr)

    # One unit never arrives.
    stray = await session.scalar(select(FwaInventory).where(FwaInventory.msisdn == msisdns[0]))
    stray.status = InventoryStatus.BLOCKED
    await session.flush()

    response = await client.post(
        f"/admin/device-pos/{device_po}/receipt", json={"confirmed": True}, headers=mpx_hdr
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "RECEIPT_INCOMPLETE"
    assert "9 of 10" in response.json()["error"]["message"]

    # Nothing was written, and the order is still open.
    po_row = await session.get(DevicePo, uuid.UUID(device_po))
    await session.refresh(po_row)
    assert po_row.status is DevicePoStatus.PERIKSA
    assert (
        await session.scalar(
            select(GoodsReceipt).where(GoodsReceipt.device_po_id == uuid.UUID(device_po))
        )
    ) is None


async def _ship_ten(
    client,
    admin_login,
    dp_admin,
    ioh_admin,
    mpx_admin,
    call_plan,
    brand,
    priced_model,
    mpx_address,
    *,
    base: int,
    imei_base: int,
):
    dp = await admin_login(dp_admin)
    ioh = await admin_login(ioh_admin)
    mpx_hdr = await admin_login(mpx_admin)
    msisdns = [str(base + i) for i in range(10)]

    po = (
        await client.post(
            "/admin/msisdn-pos",
            json={
                "callPlanId": str(call_plan.call_plan_id),
                "brandCode": brand.code,
                "qtyRequested": 10,
            },
            headers=dp,
        )
    ).json()["msisdnPoId"]
    await client.post(f"/admin/msisdn-pos/{po}/supply", json={"msisdns": msisdns}, headers=ioh)
    await client.post(
        f"/admin/msisdn-pos/{po}/pairing",
        json={"pairs": [{"msisdn": m, "imei": str(imei_base + i)} for i, m in enumerate(msisdns)]},
        headers=dp,
    )
    device_po = (
        await client.post(
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
    ).json()["devicePoId"]
    await client.post(f"/admin/device-pos/{device_po}/accept", headers=dp)
    await client.post(
        f"/admin/device-pos/{device_po}/bundles", json={"msisdns": msisdns}, headers=dp
    )
    await client.post(
        f"/admin/device-pos/{device_po}/shipment",
        json={"courierName": "J&T Express", "awb": f"JD{base}"},
        headers=dp,
    )
    return device_po, msisdns, mpx_hdr, dp


async def test_a_unit_cannot_be_allocated_twice(client, received_stock, ae, make_ae) -> None:
    """`uq_allocation_item_msisdn` is the backstop; the row lock is the mechanism."""
    _, msisdns, mpx_hdr, _ = received_stock
    other = await make_ae()

    first = await client.post(
        "/admin/allocations",
        json={"aeId": str(ae.ae_id), "mode": "MANUAL", "msisdns": msisdns[:5]},
        headers=mpx_hdr,
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        "/admin/allocations",
        json={"aeId": str(other.ae_id), "mode": "MANUAL", "msisdns": msisdns[:5]},
        headers=mpx_hdr,
    )

    assert second.status_code == 422, second.text
    assert second.json()["error"]["code"] == "BUNDLE_NOT_ALLOCATABLE"


async def test_auto_allocation_takes_the_oldest_stock_first(
    client, session, received_stock, ae
) -> None:
    """*Alokasi akan mengutamakan modem yang masuk stok lebih awal.*"""
    from datetime import UTC, datetime, timedelta

    from app.models.inventory import FwaInventory

    _, msisdns, mpx_hdr, _ = received_stock
    # Receipt stamps every unit at the same instant, so spread them to make the order
    # observable: msisdns[0] oldest, msisdns[9] newest.
    base = datetime.now(UTC) - timedelta(days=10)
    for offset, msisdn in enumerate(msisdns):
        unit = await session.scalar(select(FwaInventory).where(FwaInventory.msisdn == msisdn))
        unit.received_at = base + timedelta(days=offset)
    await session.flush()

    allocated = await client.post(
        "/admin/allocations",
        json={
            "aeId": str(ae.ae_id),
            "mode": "AUTO",
            "items": [
                {
                    "deviceModelId": str(
                        (
                            await session.scalar(
                                select(FwaInventory).where(FwaInventory.msisdn == msisdns[0])
                            )
                        ).device_model_id
                    ),
                    "qty": 3,
                }
            ],
        },
        headers=mpx_hdr,
    )

    assert allocated.status_code == 201, allocated.text
    assert sorted(allocated.json()["msisdns"]) == sorted(msisdns[:3])


async def test_allocating_more_than_is_in_stock_fails_cleanly(
    client, session, received_stock, ae
) -> None:
    from app.models.inventory import FwaInventory

    _, msisdns, mpx_hdr, _ = received_stock
    model_id = (
        await session.scalar(select(FwaInventory).where(FwaInventory.msisdn == msisdns[0]))
    ).device_model_id

    response = await client.post(
        "/admin/allocations",
        json={
            "aeId": str(ae.ae_id),
            "mode": "AUTO",
            "items": [{"deviceModelId": str(model_id), "qty": 25}],
        },
        headers=mpx_hdr,
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_STOCK"
    # Nothing partial was handed over.
    assert (await client.get("/admin/allocations", headers=mpx_hdr)).json()["meta"]["total"] == 0


async def test_an_ae_from_another_mpx_cannot_be_allocated_to(
    client, session, received_stock, make_ae
) -> None:
    _, _, mpx_hdr, _ = received_stock
    outsider = await make_ae()
    outsider.mpx_code = "MPX-SDA-02"
    await session.flush()

    response = await client.post(
        "/admin/allocations",
        json={"aeId": str(outsider.ae_id), "mode": "AUTO", "items": []},
        headers=mpx_hdr,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "AE_NOT_FOUND"


async def test_an_order_cannot_ship_before_every_bundle_is_attached(
    client,
    admin_login,
    dp_admin,
    ioh_admin,
    mpx_admin,
    call_plan,
    brand,
    priced_model,
    mpx_address,
) -> None:
    """Receipt is all-or-nothing, so a short shipment guarantees an order that can never
    be confirmed. Cheaper to refuse while the boxes are still in the warehouse."""
    dp = await admin_login(dp_admin)
    mpx_hdr = await admin_login(mpx_admin)

    device_po = (
        await client.post(
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
    ).json()["devicePoId"]
    await client.post(f"/admin/device-pos/{device_po}/accept", headers=dp)

    response = await client.post(
        f"/admin/device-pos/{device_po}/shipment",
        json={"courierName": "JNE", "awb": "JNE123"},
        headers=dp,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "ORDER_NOT_FULLY_ATTACHED"


async def test_a_milestone_cannot_be_recorded_twice(client, received_stock, session) -> None:
    from app.models.fulfilment import Shipment

    device_po, _, _mpx_hdr, dp = received_stock
    shipment = await session.scalar(
        select(Shipment).where(Shipment.device_po_id == uuid.UUID(device_po))
    )

    first = await client.post(
        f"/admin/shipments/{shipment.shipment_id}/milestones",
        json={"milestone": "IN_TRANSIT"},
        headers=dp,
    )
    second = await client.post(
        f"/admin/shipments/{shipment.shipment_id}/milestones",
        json={"milestone": "IN_TRANSIT"},
        headers=dp,
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "MILESTONE_ALREADY_RECORDED"
