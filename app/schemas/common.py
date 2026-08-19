"""Shared response shapes.

JSON bodies are camelCase; Python and the database are snake_case. The conversion is
done by Pydantic's alias generator, never by hand.

**Absent versus null.** The contract distinguishes the two, and the distinction is
load-bearing. Most optional response fields are declared as a plain type —
``reason: {type: string}``, ``accuracyM: {type: number}`` — which means the key is
*absent* when it does not apply; emitting ``null`` puts a value there that the declared
schema rejects. A handful of fields are instead declared ``oneOf [type, "null"]``,
because the client is promised the key is always there: ``activationDate`` is null until
the Gross Add feed confirms, and an app that treated a missing key as "no activation
yet" versus "field not returned" would render the wrong badge.

So the default is to drop ``None`` on the way out, and the exceptions are marked
explicitly with :func:`nullable_field`. Doing it here, once, beats remembering
``response_model_exclude_none`` on every route that ever gains an optional field.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer
from pydantic.alias_generators import to_camel

_ALWAYS_PRESENT = "x-always-present"


def nullable_field(**kwargs: Any) -> Any:
    """Declare a field the contract types as ``oneOf [..., "null"]``.

    Such a field keeps its key with an explicit ``null`` instead of being dropped.
    """
    extra = dict(kwargs.pop("json_schema_extra", {}) or {})
    extra[_ALWAYS_PRESENT] = True
    return Field(**kwargs, json_schema_extra=extra)


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        ser_json_timedelta="iso8601",
    )

    @model_serializer(mode="wrap")
    def _drop_absent_fields(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        serialised: dict[str, Any] = handler(self)
        # Both spellings, because a dump may or may not have been asked for aliases.
        keep: set[str] = set()
        for name, field in type(self).model_fields.items():
            extra = field.json_schema_extra
            if isinstance(extra, dict) and extra.get(_ALWAYS_PRESENT):
                keep.add(name)
                keep.add(field.alias or to_camel(name))
        return {key: value for key, value in serialised.items() if value is not None or key in keep}


class ErrorDetail(CamelModel):
    field: str | None = None
    issue: str | None = None


class ErrorBody(CamelModel):
    code: str = Field(description="Stable machine-readable identifier. Never localised.")
    message: str = Field(description="Human-readable, safe to show to the AE.")
    details: list[ErrorDetail] | None = None


class ErrorResponse(CamelModel):
    """The single error envelope every failing request returns."""

    error: ErrorBody


class PageMeta(CamelModel):
    page: int
    per_page: int
    total: int
    total_pages: int


class GeoPoint(CamelModel):
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    verified: bool = Field(
        default=False,
        description="Server-side check passed — accuracy within tolerance and not mocked.",
    )
    is_mocked: bool = Field(
        default=False,
        description=(
            "Android reported a mock-location provider. Recorded and flagged for review, "
            "never used to silently reject a submission."
        ),
    )

    @classmethod
    def build(
        cls,
        *,
        latitude: float,
        longitude: float,
        accuracy_m: Decimal | float | None,
        verified: bool,
        is_mocked: bool,
    ) -> GeoPoint:
        return cls(
            latitude=latitude,
            longitude=longitude,
            accuracy_m=float(accuracy_m) if accuracy_m is not None else None,
            verified=verified,
            is_mocked=is_mocked,
        )


class Paginated[T](CamelModel):
    """The list envelope: ``{"data": [...], "meta": {...}}``."""

    data: list[T]
    meta: PageMeta


class Wrapped[T](CamelModel):
    """The single-object envelope: ``{"data": ...}``.

    ``data`` survives a null: ``GET /attendance/today`` documents ``{"data": null}`` as
    meaning "not yet checked in", which an empty object would not convey.
    """

    data: T = nullable_field()


def paginated(data: list[Any], meta: dict[str, int]) -> dict[str, Any]:
    return {"data": data, "meta": meta}
