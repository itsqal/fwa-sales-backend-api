"""Admin authentication, and the audience isolation the whole extension rests on.

The isolation tests are the point of this file. The supply-chain work adds a second
principal type to a service whose every existing endpoint assumes there is only one,
and the guarantee that keeps golden rule 2 true is that the two token audiences cannot
authenticate each other's routes. If that breaks, an AE token starts reaching order
books belonging to three external companies, so it is asserted directly rather than
inferred from the login tests passing.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.models.admin import AdminUser
from app.models.enums import AdminRole, AdminStatus
from app.models.identity import AccountExecutive
from app.models.organisation import DevicePartner, Mpx
from tests.conftest import TEST_PASSWORD

# ---------------------------------------------------------------------------
# Audience isolation
# ---------------------------------------------------------------------------

# Every AE-audience route the mobile app actually calls. An admin token must reach
# none of them, whatever the shape of the request.
AE_ROUTES = [
    ("GET", "/me"),
    ("GET", "/customers"),
    ("GET", "/inventory/me"),
    ("GET", "/inventory/msisdn/6285882724305"),
    ("GET", "/activations"),
    ("GET", "/attendance/today"),
    ("GET", "/reports/summary"),
    ("GET", "/sync/bootstrap"),
]

ADMIN_ROUTES = [
    ("GET", "/admin/me"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
async def test_an_ae_token_reaches_nothing_under_admin(
    client: AsyncClient, auth_headers: dict[str, str], method: str, path: str
) -> None:
    """An AE token carries no `aud`, so the admin decoder rejects it outright.

    This must be a 401 from the token layer, not a 403 from a role check — the AE is
    not a dashboard principal with insufficient rights, it is not a dashboard
    principal at all.
    """
    response = await client.request(method, path, headers=auth_headers)

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


@pytest.mark.parametrize(("method", "path"), AE_ROUTES)
async def test_an_admin_token_reaches_nothing_outside_admin(
    client: AsyncClient, admin_auth_headers: dict[str, str], method: str, path: str
) -> None:
    """An admin token carries `aud: "admin"`, which the AE decoder never asks for.

    PyJWT raises InvalidAudienceError rather than returning a payload, so the request
    fails before any handler — and before any query scoped to `sub` — can run.
    """
    response = await client.request(method, path, headers=admin_auth_headers)

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


async def test_admin_routes_reject_a_request_with_no_token_at_all(client: AsyncClient) -> None:
    response = await client.get("/admin/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "MISSING_TOKEN"


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


async def test_login_returns_tokens_and_the_org_binding(
    client: AsyncClient, dp_admin: AdminUser, device_partner: DevicePartner
) -> None:
    response = await client.post(
        "/admin/auth/login", json={"username": dp_admin.username, "password": TEST_PASSWORD}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expiresIn"] == 900
    assert body["mustChangePassword"] is False

    profile = body["profile"]
    assert profile["role"] == "DP_ADMIN"
    assert profile["fullName"] == "Atha Marcella"
    assert profile["organisation"]["type"] == "DEVICE_PARTNER"
    assert profile["organisation"]["code"] == device_partner.code


async def test_username_is_matched_case_insensitively(
    client: AsyncClient, make_admin, device_partner: DevicePartner
) -> None:
    """Same rule as an AE code, backed by uq_admin_username_ci."""
    await make_admin(role=AdminRole.DP_ADMIN, device_partner=device_partner, username="dp.advan")

    response = await client.post(
        "/admin/auth/login", json={"username": "DP.ADVAN", "password": TEST_PASSWORD}
    )

    assert response.status_code == 200, response.text


async def test_wrong_password_and_unknown_user_are_indistinguishable(
    client: AsyncClient, dp_admin: AdminUser
) -> None:
    """Distinguishing the two would turn the login form into an account enumerator."""
    wrong_password = await client.post(
        "/admin/auth/login", json={"username": dp_admin.username, "password": "not-the-password"}
    )
    unknown_user = await client.post(
        "/admin/auth/login", json={"username": "no.such.admin", "password": TEST_PASSWORD}
    )

    assert wrong_password.status_code == unknown_user.status_code == 401
    assert (
        wrong_password.json()["error"]["code"]
        == unknown_user.json()["error"]["code"]
        == "INVALID_CREDENTIALS"
    )
    assert wrong_password.json()["error"]["message"] == unknown_user.json()["error"]["message"]


async def test_a_suspended_admin_cannot_log_in(
    client: AsyncClient, make_admin, device_partner: DevicePartner
) -> None:
    admin = await make_admin(
        role=AdminRole.DP_ADMIN, device_partner=device_partner, status=AdminStatus.SUSPENDED
    )

    response = await client.post(
        "/admin/auth/login", json={"username": admin.username, "password": TEST_PASSWORD}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ACCOUNT_INACTIVE"


async def test_repeated_failures_are_rate_limited(client: AsyncClient, dp_admin: AdminUser) -> None:
    """The limiter is keyed per username and address, and is separate from the AE one."""
    for _ in range(5):
        await client.post(
            "/admin/auth/login", json={"username": dp_admin.username, "password": "wrong"}
        )

    response = await client.post(
        "/admin/auth/login", json={"username": dp_admin.username, "password": TEST_PASSWORD}
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "TOO_MANY_LOGIN_ATTEMPTS"
    assert "Retry-After" in response.headers


# ---------------------------------------------------------------------------
# The org binding, per role
# ---------------------------------------------------------------------------


async def test_ioh_admin_is_bound_to_no_organisation(
    client: AsyncClient, admin_login, ioh_admin: AdminUser
) -> None:
    """IOH is global-read by design, so the dashboard renders no counterparty label."""
    headers = await admin_login(ioh_admin)

    response = await client.get("/admin/me", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role"] == "IOH_ADMIN"
    assert body["organisation"] is None


async def test_mpx_admin_carries_the_topbar_fields(
    client: AsyncClient, admin_login, mpx_admin: AdminUser, mpx: Mpx
) -> None:
    headers = await admin_login(mpx_admin)

    response = await client.get("/admin/me", headers=headers)

    assert response.status_code == 200, response.text
    organisation = response.json()["organisation"]
    assert organisation["type"] == "MPX"
    assert organisation["code"] == mpx.code
    # Rendered under the user name in the MPX topbar.
    assert organisation["legalName"] == "PT Internet Rakyat Makmur"
    assert organisation["circle"] == "SUMATERA"


# ---------------------------------------------------------------------------
# Refresh and logout
# ---------------------------------------------------------------------------


async def test_refresh_rotates_the_token_and_revokes_the_old_one(
    client: AsyncClient, dp_admin: AdminUser
) -> None:
    login = await client.post(
        "/admin/auth/login", json={"username": dp_admin.username, "password": TEST_PASSWORD}
    )
    original = login.json()["refreshToken"]

    rotated = await client.post("/admin/auth/refresh", json={"refreshToken": original})
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["refreshToken"] != original

    replayed = await client.post("/admin/auth/refresh", json={"refreshToken": original})
    assert replayed.status_code == 401
    assert replayed.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"


async def test_logout_ends_the_session_before_the_access_token_expires(
    client: AsyncClient, dp_admin: AdminUser
) -> None:
    """A revoked session must stop working immediately, not in up to 15 minutes."""
    login = await client.post(
        "/admin/auth/login", json={"username": dp_admin.username, "password": TEST_PASSWORD}
    )
    headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}

    assert (await client.get("/admin/me", headers=headers)).status_code == 200

    logout = await client.post("/admin/auth/logout", headers=headers)
    assert logout.status_code == 204

    after = await client.get("/admin/me", headers=headers)
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "SESSION_REVOKED"


async def test_logout_does_not_end_this_admins_other_sessions(
    client: AsyncClient, dp_admin: AdminUser
) -> None:
    first = await client.post(
        "/admin/auth/login",
        json={"username": dp_admin.username, "password": TEST_PASSWORD, "deviceLabel": "laptop"},
    )
    second = await client.post(
        "/admin/auth/login",
        json={"username": dp_admin.username, "password": TEST_PASSWORD, "deviceLabel": "desktop"},
    )
    first_headers = {"Authorization": f"Bearer {first.json()['accessToken']}"}
    second_headers = {"Authorization": f"Bearer {second.json()['accessToken']}"}

    await client.post("/admin/auth/logout", headers=first_headers)

    assert (await client.get("/admin/me", headers=second_headers)).status_code == 200


async def test_an_ae_refresh_token_cannot_be_redeemed_for_an_admin_session(
    client: AsyncClient, ae: AccountExecutive
) -> None:
    """The two token tables are separate, so an AE refresh token hashes to nothing here.

    Worth asserting explicitly: both tables store the same SHA-256 digest format, and a
    single shared lookup would have made this work.
    """
    login = await client.post("/auth/login", json={"aeCode": ae.ae_code, "password": TEST_PASSWORD})
    ae_refresh = login.json()["refreshToken"]

    response = await client.post("/admin/auth/refresh", json={"refreshToken": ae_refresh})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"
