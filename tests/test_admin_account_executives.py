"""The AE directory, and the brand scope that resolves UI Review issue #9."""

from __future__ import annotations

from httpx import AsyncClient

from app.models.enums import BrandScope, InventoryStatus


async def test_an_mpx_sees_only_its_own_account_executives(
    client: AsyncClient, admin_login, mpx_admin, make_ae, session
) -> None:
    """Scoped on account_executive.mpx_code, from the token. No parameter widens it."""
    mine = await make_ae()
    theirs = await make_ae()
    theirs.mpx_code = "MPX-SDA-02"
    await session.flush()
    headers = await admin_login(mpx_admin)

    response = await client.get("/admin/account-executives", headers=headers)

    assert response.status_code == 200, response.text
    codes = {row["aeCode"] for row in response.json()["data"]}
    assert mine.ae_code in codes
    assert theirs.ae_code not in codes


async def test_another_mpxs_ae_reads_as_absent_not_forbidden(
    client: AsyncClient, admin_login, mpx_admin, make_ae, session
) -> None:
    theirs = await make_ae()
    theirs.mpx_code = "MPX-SDA-02"
    await session.flush()
    headers = await admin_login(mpx_admin)

    response = await client.get(f"/admin/account-executives/{theirs.ae_id}", headers=headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AE_NOT_FOUND"


async def test_a_device_partner_cannot_browse_account_executives(
    client: AsyncClient, admin_auth_headers
) -> None:
    response = await client.get("/admin/account-executives", headers=admin_auth_headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_NOT_PERMITTED"


async def test_the_tentang_ae_panel_renders_id_brand_and_branch(
    client: AsyncClient, admin_login, mpx_admin, make_ae, session
) -> None:
    """Issue #9: HYBRID means the AE may sell either brand. Confirmed 2026-09-01."""
    ae = await make_ae()
    ae.brand_scope = BrandScope.HYBRID
    await session.flush()
    headers = await admin_login(mpx_admin)

    response = await client.get(f"/admin/account-executives/{ae.ae_id}", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["aeCode"] == ae.ae_code
    assert body["brandScope"] == "HYBRID"
    assert body["region"]["regionName"] == "Bengkulu"


async def test_an_unrecorded_brand_scope_is_null_rather_than_omitted(
    client: AsyncClient, admin_login, mpx_admin, ae
) -> None:
    """Every AE that exists today is NULL. The panel always renders a Brand row, so the
    key must be present — "not recorded" has to be distinguishable from "not returned"."""
    headers = await admin_login(mpx_admin)

    response = await client.get(f"/admin/account-executives/{ae.ae_id}", headers=headers)

    assert response.status_code == 200, response.text
    assert "brandScope" in response.json()
    assert response.json()["brandScope"] is None


async def test_the_panel_counts_only_stock_still_on_the_shelf(
    client: AsyncClient, admin_login, mpx_admin, ae, make_inventory
) -> None:
    """Activated units have left the AE's stock and must not inflate the count."""
    await make_inventory(ae=ae, status=InventoryStatus.ALLOCATED)
    await make_inventory(ae=ae, status=InventoryStatus.ALLOCATED)
    await make_inventory(ae=ae, status=InventoryStatus.ACTIVATED)
    headers = await admin_login(mpx_admin)

    response = await client.get(f"/admin/account-executives/{ae.ae_id}", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["allocatedTotal"] == 2


async def test_hybrid_is_not_a_brand(client: AsyncClient, admin_auth_headers) -> None:
    """HYBRID is a capability of a person. If it ever appears in the brand reference
    table it will leak into every brandCode column in the supply chain."""
    response = await client.get("/admin/reference/brands", headers=admin_auth_headers)

    codes = {row["code"] for row in response.json()["data"]}
    assert codes == {"IM3", "3ID"}
    assert "HYBRID" not in codes
