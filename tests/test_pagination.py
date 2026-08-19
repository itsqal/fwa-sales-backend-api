"""Pagination edge cases found by fuzzing the contract.

The contract puts a minimum on `page` but no maximum, so an enormous page number is a
schema-compliant request. It must produce an empty page, never a 500.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.pagination import MAX_OFFSET, Pagination

HUGE_PAGE = 28801501667628062482502778880  # from the schemathesis run that caught this


def test_offset_is_clamped_below_the_int64_bind_limit() -> None:
    """Unclamped, this overflows the bigint parameter behind OFFSET and the driver
    raises before PostgreSQL ever sees the query."""
    pagination = Pagination(page=HUGE_PAGE, per_page=93)

    assert pagination.offset == MAX_OFFSET
    assert pagination.offset < 2**63


def test_ordinary_offsets_are_untouched() -> None:
    assert Pagination(page=1, per_page=20).offset == 0
    assert Pagination(page=3, per_page=20).offset == 40


@pytest.mark.parametrize(
    "path",
    ["/customers", "/activations", "/attendance", "/inventory/me", "/reports/incentives"],
)
async def test_an_enormous_page_returns_an_empty_page_not_a_500(
    client: AsyncClient, auth_headers, path: str
) -> None:
    response = await client.get(f"{path}?page={HUGE_PAGE}&perPage=93", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["data"] == []


@pytest.mark.parametrize(("page", "per_page"), [(0, 20), (-1, 20), (1, 0), (1, 101)])
async def test_out_of_range_pagination_is_422(
    client: AsyncClient, auth_headers, page: int, per_page: int
) -> None:
    response = await client.get(f"/customers?page={page}&perPage={per_page}", headers=auth_headers)

    assert response.status_code == 422
