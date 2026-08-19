"""The single error envelope and the exception hierarchy that produces it.

Every error response on this API is::

    {"error": {"code": "...", "message": "...", "details": [...]}}

``code`` is a stable SCREAMING_SNAKE identifier. Clients branch on it, so it is
never localised or reworded. ``message`` is human-readable and safe to show an AE,
which means it must never carry PII — no full MSISDNs, names or addresses.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every error this API returns deliberately."""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: list[dict[str, str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details
        self.headers = headers

    def to_payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


class UnauthorizedError(AppError):
    status_code = 401
    code = "UNAUTHORIZED"


class ForbiddenError(AppError):
    """Reserved for the service-credential path only.

    AE-facing endpoints never return 403 for an ownership failure — that would
    confirm the record exists. They return 404 (see NotFoundError).
    """

    status_code = 403
    code = "FORBIDDEN"


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"


class ValidationFailedError(AppError):
    status_code = 422
    code = "VALIDATION_FAILED"


class TooManyRequestsError(AppError):
    status_code = 429
    code = "TOO_MANY_REQUESTS"
