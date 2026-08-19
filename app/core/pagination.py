"""``page``/``perPage`` to ``LIMIT``/``OFFSET``, plus the list envelope's meta block."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Annotated, Any, TypeVar

from fastapi import Query

T = TypeVar("T")


# The contract puts a minimum on `page` but no maximum, so a schema-compliant request
# may carry an arbitrarily large page number. Multiplied out, that overflows the int64
# bind parameter behind OFFSET and the driver raises — a 500 for a request the contract
# says is valid. Clamping instead returns an honest empty page. The ceiling is far past
# any real dataset; nothing legitimate is ever truncated by it.
MAX_OFFSET = 2**31


@dataclass(frozen=True, slots=True)
class Pagination:
    page: int
    per_page: int

    @property
    def limit(self) -> int:
        return self.per_page

    @property
    def offset(self) -> int:
        return min((self.page - 1) * self.per_page, MAX_OFFSET)

    def meta(self, total: int) -> dict[str, int]:
        return {
            "page": self.page,
            "perPage": self.per_page,
            "total": total,
            "totalPages": ceil(total / self.per_page) if self.per_page else 0,
        }

    def envelope(self, items: list[T], total: int) -> dict[str, Any]:
        return {"data": items, "meta": self.meta(total)}


def get_pagination(
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(alias="perPage", ge=1, le=100)] = 20,
) -> Pagination:
    return Pagination(page=page, per_page=per_page)
