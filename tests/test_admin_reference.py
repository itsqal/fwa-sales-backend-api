"""Reference-data endpoints, and the role boundaries on two of them."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.models.admin import AdminUser


async def test_call_plans_expose_the_balance_discriminator(
    client: AsyncClient, admin_auth_headers: dict[str, str]
) -> None:
    """*Saldo Mobo* is a balance top-up, not a data bundle.

    The client is expected to branch on `kind`; this asserts the API actually gives it
    something to branch on, rather than a null quota it would have to guess about.
    """
    response = await client.get("/admin/reference/call-plans", headers=admin_auth_headers)

    assert response.status_code == 200, response.text
    plans = {plan["code"]: plan for plan in response.json()["data"]}

    assert plans["DATA_50GB"]["kind"] == "DATA"
    assert plans["DATA_50GB"]["quotaGb"] == 50
    assert plans["SALDO_MOBO"]["kind"] == "BALANCE"
    assert "quotaGb" not in plans["SALDO_MOBO"]


async def test_call_plans_are_returned_in_display_order(
    client: AsyncClient, admin_auth_headers: dict[str, str]
) -> None:
    response = await client.get("/admin/reference/call-plans", headers=admin_auth_headers)

    codes = [plan["code"] for plan in response.json()["data"]]
    assert codes == ["DATA_50GB", "DATA_75GB", "DATA_150GB", "SALDO_MOBO"]


async def test_brands_carry_both_names(
    client: AsyncClient, admin_auth_headers: dict[str, str]
) -> None:
    """The mockups used *IM3* and *Gerai IM3* interchangeably. Both are returned so a
    screen picks deliberately rather than by whichever mockup was copied."""
    response = await client.get("/admin/reference/brands", headers=admin_auth_headers)

    assert response.status_code == 200, response.text
    brands = {brand["code"]: brand for brand in response.json()["data"]}
    assert brands["IM3"]["displayName"] == "IM3"
    assert brands["IM3"]["outletName"] == "Gerai IM3"
    assert brands["3ID"]["outletName"] == "3Store"


def test_the_confirmed_catalogue_is_exactly_the_three_ae_seed_models() -> None:
    """Confirmed 2026-09-01: the catalogue is the existing AE seed and nothing else.

    Pinned here rather than against the API because the catalogue is seed data, not
    migration data. Seeding the mockup names alongside these would split inventory
    across two rows for one physical device, and the AE app would start resolving a
    model the dashboard has never heard of.
    """
    from app.scripts.seed import DEVICE_MODELS

    codes = {model_code for model_code, *_ in DEVICE_MODELS}

    assert codes == {"HKM 127+", "RABIT CPE-XR", "ADVAN V1 PRO"}
    # Names that appear only in the web mockups. Deliberately not seeded.
    assert codes.isdisjoint({"ZTE K12", "Rabit", "Rabit Unlimited", "HKM 131 PRO"})


async def test_a_model_reports_its_partner_and_a_null_price(
    client: AsyncClient, admin_auth_headers: dict[str, str], session, device_partner
) -> None:
    """`brand` is the manufacturer; `devicePartner` is the structured form of it.

    `listPriceIdr` must be present and null rather than omitted: a device picker has
    to tell "not priced, cannot be ordered" apart from "the field was not returned".
    """
    from app.models.inventory import DeviceModel

    session.add(
        DeviceModel(
            model_code="ADVAN V1 PRO TEST",
            brand="ADVAN",
            sku="SKU-ADV-V1P",
            device_partner_id=device_partner.device_partner_id,
        )
    )
    await session.flush()

    response = await client.get("/admin/reference/device-models", headers=admin_auth_headers)

    assert response.status_code == 200, response.text
    model = next(row for row in response.json()["data"] if row["modelCode"] == "ADVAN V1 PRO TEST")
    assert model["brand"] == "ADVAN"
    assert model["devicePartner"]["code"] == "ADVAN"
    assert "listPriceIdr" in model
    assert model["listPriceIdr"] is None


async def test_an_inactive_model_is_hidden_unless_asked_for(
    client: AsyncClient, admin_auth_headers: dict[str, str], session
) -> None:
    from app.models.inventory import DeviceModel

    session.add(DeviceModel(model_code="RETIRED MODEL", brand="HKM", is_active=False))
    await session.flush()

    hidden = await client.get("/admin/reference/device-models", headers=admin_auth_headers)
    shown = await client.get(
        "/admin/reference/device-models?includeInactive=true", headers=admin_auth_headers
    )

    assert "RETIRED MODEL" not in {row["modelCode"] for row in hidden.json()["data"]}
    assert "RETIRED MODEL" in {row["modelCode"] for row in shown.json()["data"]}


# ---------------------------------------------------------------------------
# Role boundaries
# ---------------------------------------------------------------------------


async def test_a_device_partner_cannot_enumerate_its_competitors(
    client: AsyncClient, admin_auth_headers: dict[str, str]
) -> None:
    response = await client.get("/admin/reference/device-partners", headers=admin_auth_headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_NOT_PERMITTED"


ALL_PARTNER_CODES = {"ADVAN", "RABIT", "ZTE", "HKM", "HUAWEI", "BANGGA"}


async def test_ioh_may_list_device_partners(
    client: AsyncClient, admin_login, ioh_admin: AdminUser
) -> None:
    headers = await admin_login(ioh_admin)

    response = await client.get("/admin/reference/device-partners", headers=headers)

    assert response.status_code == 200, response.text
    assert {row["code"] for row in response.json()["data"]} >= ALL_PARTNER_CODES


async def test_mpx_may_list_device_partners(
    client: AsyncClient, admin_login, mpx_admin: AdminUser
) -> None:
    """An MPX orders from them, so it needs the list."""
    headers = await admin_login(mpx_admin)

    response = await client.get("/admin/reference/device-partners", headers=headers)

    assert response.status_code == 200, response.text
    assert {row["code"] for row in response.json()["data"]} >= ALL_PARTNER_CODES


async def test_an_mpx_admin_cannot_enumerate_the_other_stock_points(
    client: AsyncClient, admin_login, mpx_admin: AdminUser
) -> None:
    """It already knows which MPX it is — that is on GET /admin/me."""
    headers = await admin_login(mpx_admin)

    response = await client.get("/admin/reference/mpx", headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_NOT_PERMITTED"


async def test_a_device_partner_may_list_mpx_stock_points(
    client: AsyncClient, admin_auth_headers: dict[str, str]
) -> None:
    """A DP ships to them, so it needs their names."""
    response = await client.get("/admin/reference/mpx", headers=admin_auth_headers)

    assert response.status_code == 200, response.text
    rows = {row["code"]: row for row in response.json()["data"]}
    assert rows["MPX-BKL-01"]["legalName"] == "PT Internet Rakyat Makmur"
    assert rows["MPX-BKL-01"]["circle"] == "SUMATERA"


@pytest.mark.parametrize(
    "path",
    [
        "/admin/reference/call-plans",
        "/admin/reference/brands",
        "/admin/reference/device-models",
        "/admin/reference/device-partners",
        "/admin/reference/mpx",
    ],
)
async def test_reference_data_is_not_reachable_with_an_ae_token(
    client: AsyncClient, auth_headers: dict[str, str], path: str
) -> None:
    """Master data for three external companies is not AE-visible, role aside."""
    response = await client.get(path, headers=auth_headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"
