"""Auth endpoints: login, refresh rotation, logout, profile."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.models.enums import AeStatus
from tests.conftest import TEST_PASSWORD


async def test_login_returns_tokens_and_profile(client: AsyncClient, ae) -> None:
    response = await client.post(
        "/auth/login",
        json={"aeCode": ae.ae_code, "password": TEST_PASSWORD, "deviceLabel": "Samsung A15"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["accessToken"] and body["refreshToken"]
    assert body["expiresIn"] == 900
    assert body["mustChangePassword"] is False
    assert body["profile"]["aeCode"] == ae.ae_code
    assert body["profile"]["role"] == "AE"
    assert body["profile"]["workShift"] == {"start": "08:00", "end": "18:00"}


@pytest.mark.parametrize("transform", [str.lower, str.upper, str.swapcase])
async def test_login_is_case_insensitive(client: AsyncClient, ae, transform) -> None:
    """Brief slide 9: "Insensitive Case". Backed by the uq_ae_code_ci functional index."""
    response = await client.post(
        "/auth/login", json={"aeCode": transform(ae.ae_code), "password": TEST_PASSWORD}
    )
    assert response.status_code == 200


async def test_login_with_wrong_password_is_401(client: AsyncClient, ae) -> None:
    response = await client.post(
        "/auth/login", json={"aeCode": ae.ae_code, "password": "wrong-password"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_login_does_not_reveal_whether_the_ae_code_exists(client: AsyncClient, ae) -> None:
    """AE codes are guessable by design, so the two failures must be indistinguishable."""
    unknown = await client.post(
        "/auth/login", json={"aeCode": "AE-DOESNOTEXIST9", "password": "whatever"}
    )
    wrong_password = await client.post(
        "/auth/login", json={"aeCode": ae.ae_code, "password": "whatever"}
    )

    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json() == wrong_password.json()


async def test_suspended_ae_cannot_log_in(client: AsyncClient, make_ae) -> None:
    suspended = await make_ae(status=AeStatus.SUSPENDED)

    response = await client.post(
        "/auth/login", json={"aeCode": suspended.ae_code, "password": TEST_PASSWORD}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ACCOUNT_INACTIVE"


async def test_login_is_rate_limited(client: AsyncClient, ae) -> None:
    for _ in range(5):
        await client.post("/auth/login", json={"aeCode": ae.ae_code, "password": "nope"})

    response = await client.post(
        "/auth/login", json={"aeCode": ae.ae_code, "password": TEST_PASSWORD}
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "TOO_MANY_LOGIN_ATTEMPTS"
    assert "Retry-After" in response.headers


async def test_must_change_password_is_surfaced(client: AsyncClient, make_ae) -> None:
    fresh = await make_ae(must_change_pw=True)

    response = await client.post(
        "/auth/login", json={"aeCode": fresh.ae_code, "password": TEST_PASSWORD}
    )

    assert response.json()["mustChangePassword"] is True


async def test_refresh_rotates_the_token(client: AsyncClient, ae) -> None:
    login = await client.post("/auth/login", json={"aeCode": ae.ae_code, "password": TEST_PASSWORD})
    original = login.json()["refreshToken"]

    refreshed = await client.post("/auth/refresh", json={"refreshToken": original})

    assert refreshed.status_code == 200
    assert refreshed.json()["refreshToken"] != original

    # The old token is dead the moment it is exchanged.
    replayed = await client.post("/auth/refresh", json={"refreshToken": original})
    assert replayed.status_code == 401
    assert replayed.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"


async def test_refresh_rejects_an_unknown_token(client: AsyncClient) -> None:
    response = await client.post("/auth/refresh", json={"refreshToken": "not-a-real-token"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"


async def test_logout_revokes_the_calling_session_only(client: AsyncClient, ae) -> None:
    phone = await client.post(
        "/auth/login",
        json={"aeCode": ae.ae_code, "password": TEST_PASSWORD, "deviceLabel": "phone"},
    )
    tablet = await client.post(
        "/auth/login",
        json={"aeCode": ae.ae_code, "password": TEST_PASSWORD, "deviceLabel": "tablet"},
    )
    phone_headers = {"Authorization": f"Bearer {phone.json()['accessToken']}"}
    tablet_headers = {"Authorization": f"Bearer {tablet.json()['accessToken']}"}

    assert (await client.post("/auth/logout", headers=phone_headers)).status_code == 204

    # The revoked device stops working immediately rather than at token expiry.
    assert (await client.get("/me", headers=phone_headers)).status_code == 401
    assert (await client.get("/me", headers=tablet_headers)).status_code == 200


async def test_me_returns_the_signed_in_profile(client: AsyncClient, ae, auth_headers) -> None:
    response = await client.get("/me", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["aeId"] == str(ae.ae_id)


@pytest.mark.parametrize(
    ("headers", "expected_code"),
    [
        ({}, "MISSING_TOKEN"),
        ({"Authorization": "Bearer garbage"}, "INVALID_TOKEN"),
    ],
)
async def test_me_rejects_bad_auth(client: AsyncClient, headers, expected_code) -> None:
    response = await client.get("/me", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == expected_code


async def test_login_validation_failure_uses_the_error_envelope(client: AsyncClient) -> None:
    response = await client.post("/auth/login", json={"aeCode": ""})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["error"]["details"]
