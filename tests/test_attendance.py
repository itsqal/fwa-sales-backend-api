"""Attendance (CICO), including the signed-URL delivery of the selfie."""

from __future__ import annotations

import uuid

from httpx import AsyncClient

# A one-pixel PNG. Small enough to keep the suite fast, real enough to be a file.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def _files(filename: str = "selfie.jpg") -> dict:
    return {"photo": (filename, PNG_BYTES, "image/jpeg")}


def _form(**overrides) -> dict:
    data = {"latitude": "-6.2314728", "longitude": "106.9348415", "isMocked": "false"}
    data.update({key: str(value) for key, value in overrides.items()})
    return data


async def test_check_in_stores_the_selfie_and_returns_a_signed_url(
    client: AsyncClient, auth_headers, today
) -> None:
    response = await client.post(
        "/attendance/check-in", data=_form(), files=_files(), headers=auth_headers
    )

    assert response.status_code == 201
    body = response.json()
    assert body["attendanceDate"] == today.isoformat()
    assert body["checkInAt"] is not None
    assert body["approvalStatus"] == "AUTO_APPROVED"
    # Not a public bucket path: a signed, expiring reference.
    assert "/v1/files/" in body["checkInPhotoUrl"]


async def test_the_signed_url_serves_the_file_without_a_bearer_token(
    client: AsyncClient, auth_headers
) -> None:
    created = await client.post(
        "/attendance/check-in", data=_form(), files=_files(), headers=auth_headers
    )
    url = created.json()["checkInPhotoUrl"]

    response = await client.get(url)

    assert response.status_code == 200
    assert response.content == PNG_BYTES


async def test_a_tampered_token_is_404(client: AsyncClient, auth_headers) -> None:
    created = await client.post(
        "/attendance/check-in", data=_form(), files=_files(), headers=auth_headers
    )
    url = created.json()["checkInPhotoUrl"]

    response = await client.get(url[:-4] + "AAAA")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "FILE_NOT_FOUND"


async def test_geofence_is_not_evaluated_while_the_radius_is_undecided(
    client: AsyncClient, auth_headers
) -> None:
    """Both the tolerance and a reference point to measure from are missing, so the
    server reports nothing rather than inventing an answer a supervisor would read as
    fact. The contract types withinGeofence as a plain boolean, so "not evaluated" is
    the key being absent — not a null that a client would coerce to False."""
    response = await client.post(
        "/attendance/check-in", data=_form(), files=_files(), headers=auth_headers
    )

    assert "withinGeofence" not in response.json()
    assert response.json()["approvalStatus"] == "AUTO_APPROVED"


async def test_check_in_requires_a_photo(client: AsyncClient, auth_headers) -> None:
    response = await client.post("/attendance/check-in", data=_form(), headers=auth_headers)

    assert response.status_code == 422


async def test_check_in_rejects_an_unsupported_file_type(client: AsyncClient, auth_headers) -> None:
    response = await client.post(
        "/attendance/check-in",
        data=_form(),
        files={"photo": ("payload.exe", PNG_BYTES, "application/octet-stream")},
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"


async def test_check_in_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/attendance/check-in", data=_form(), files=_files())

    assert response.status_code == 401


async def test_second_check_in_on_the_same_day_is_409(client: AsyncClient, auth_headers) -> None:
    await client.post("/attendance/check-in", data=_form(), files=_files(), headers=auth_headers)

    response = await client.post(
        "/attendance/check-in", data=_form(), files=_files(), headers=auth_headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ALREADY_CHECKED_IN"


async def test_check_in_idempotency_replay(client: AsyncClient, auth_headers) -> None:
    headers = {**auth_headers, "Idempotency-Key": str(uuid.uuid4())}

    first = await client.post("/attendance/check-in", data=_form(), files=_files(), headers=headers)
    second = await client.post(
        "/attendance/check-in", data=_form(), files=_files(), headers=headers
    )

    assert first.status_code == second.status_code == 201
    assert first.json()["attendanceId"] == second.json()["attendanceId"]


async def test_two_aes_may_both_check_in_today(
    client: AsyncClient, auth_headers, other_auth_headers
) -> None:
    mine = await client.post(
        "/attendance/check-in", data=_form(), files=_files(), headers=auth_headers
    )
    theirs = await client.post(
        "/attendance/check-in", data=_form(), files=_files(), headers=other_auth_headers
    )

    assert mine.status_code == theirs.status_code == 201


async def test_check_out(client: AsyncClient, auth_headers) -> None:
    await client.post("/attendance/check-in", data=_form(), files=_files(), headers=auth_headers)

    response = await client.post(
        "/attendance/check-out",
        json={"latitude": -6.23, "longitude": 106.93},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["checkOutAt"] is not None


async def test_check_out_without_a_check_in_is_409(client: AsyncClient, auth_headers) -> None:
    response = await client.post(
        "/attendance/check-out",
        json={"latitude": -6.23, "longitude": 106.93},
        headers=auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NO_OPEN_CHECK_IN"


async def test_second_check_out_is_409(client: AsyncClient, auth_headers) -> None:
    await client.post("/attendance/check-in", data=_form(), files=_files(), headers=auth_headers)
    payload = {"latitude": -6.23, "longitude": 106.93}
    await client.post("/attendance/check-out", json=payload, headers=auth_headers)

    response = await client.post("/attendance/check-out", json=payload, headers=auth_headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ALREADY_CHECKED_OUT"


async def test_check_out_validation(client: AsyncClient, auth_headers) -> None:
    response = await client.post(
        "/attendance/check-out", json={"latitude": 999, "longitude": 0}, headers=auth_headers
    )

    assert response.status_code == 422


async def test_today_is_null_before_check_in(client: AsyncClient, auth_headers) -> None:
    response = await client.get("/attendance/today", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"data": None}


async def test_today_after_check_in(client: AsyncClient, auth_headers) -> None:
    await client.post("/attendance/check-in", data=_form(), files=_files(), headers=auth_headers)

    response = await client.get("/attendance/today", headers=auth_headers)

    assert response.json()["data"]["checkInAt"] is not None


async def test_history_is_scoped_to_the_signed_in_ae(
    client: AsyncClient, auth_headers, other_auth_headers
) -> None:
    await client.post(
        "/attendance/check-in", data=_form(), files=_files(), headers=other_auth_headers
    )

    response = await client.get("/attendance", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["meta"]["total"] == 0


async def test_history_lists_own_records(client: AsyncClient, auth_headers) -> None:
    await client.post("/attendance/check-in", data=_form(), files=_files(), headers=auth_headers)

    response = await client.get("/attendance", headers=auth_headers)

    assert response.json()["meta"]["total"] == 1
