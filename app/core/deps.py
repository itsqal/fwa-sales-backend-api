"""Shared FastAPI dependencies.

The important one is :func:`get_current_ae`. AE identity comes from the JWT and from
nowhere else — no endpoint accepts an ``aeId`` in a body or query string, so a client
has no way to write on another AE's behalf.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import jwt
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, UnauthorizedError, ValidationFailedError
from app.core.security import constant_time_equals, decode_access_token
from app.db.session import session_scope
from app.models.enums import AeStatus
from app.models.identity import AccountExecutive, AuthRefreshToken

bearer_scheme = HTTPBearer(auto_error=False, description="Short-lived access token.")


async def get_db() -> AsyncIterator[AsyncSession]:
    async for session in session_scope():
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


async def get_current_ae(
    session: DbSession,
    settings: AppSettings,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AccountExecutive:
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError(
            "Sign in to continue.",
            code="MISSING_TOKEN",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_access_token(settings, credentials.credentials)
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError(
            "Your session has expired. Sign in again.",
            code="TOKEN_EXPIRED",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.PyJWTError as exc:
        raise UnauthorizedError(
            "Your session is no longer valid. Sign in again.",
            code="INVALID_TOKEN",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        ae_id = uuid.UUID(str(payload["sub"]))
        session_id = uuid.UUID(str(payload["sid"]))
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("Your session is no longer valid. Sign in again.") from exc

    ae = await session.get(AccountExecutive, ae_id)
    if ae is None:
        raise UnauthorizedError("Your session is no longer valid. Sign in again.")

    if ae.status is not AeStatus.ACTIVE:
        raise UnauthorizedError(
            "This account is not active. Contact your supervisor.",
            code="ACCOUNT_INACTIVE",
        )

    # A revoked session must stop working before its access token expires, otherwise
    # "log out on a lost phone" is only advisory for up to 15 minutes.
    revoked = await session.scalar(
        select(AuthRefreshToken.token_id).where(
            AuthRefreshToken.token_id == session_id,
            AuthRefreshToken.revoked_at.is_(None),
        )
    )
    if revoked is None:
        raise UnauthorizedError(
            "Your session has ended. Sign in again.",
            code="SESSION_REVOKED",
        )

    return ae


CurrentAe = Annotated[AccountExecutive, Depends(get_current_ae)]


def get_session_id(
    settings: AppSettings,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> uuid.UUID:
    """The refresh-token id embedded in the access token — the caller's device session."""
    if credentials is None:
        raise UnauthorizedError("Sign in to continue.", code="MISSING_TOKEN")
    try:
        payload = decode_access_token(settings, credentials.credentials)
        return uuid.UUID(str(payload["sid"]))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise UnauthorizedError("Your session is no longer valid.") from exc


async def require_service_credential(
    settings: AppSettings,
    x_service_key: Annotated[str | None, Header(alias="X-Service-Key")] = None,
) -> None:
    """Guard for ``POST /internal/ga-events``.

    This is a server-to-server path and must also be unreachable from the public app
    path at the reverse proxy — the shared secret is the second line, not the first.
    """
    if x_service_key is None or not constant_time_equals(x_service_key, settings.service_api_key):
        raise ForbiddenError(
            "This endpoint requires a service credential.",
            code="INVALID_SERVICE_CREDENTIAL",
        )


def get_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> uuid.UUID | None:
    """Parse the ``Idempotency-Key`` header, which the offline outbox sets on retries."""
    if idempotency_key is None or not idempotency_key.strip():
        return None
    try:
        return uuid.UUID(idempotency_key.strip())
    except ValueError as exc:
        raise ValidationFailedError(
            "Idempotency-Key must be a UUID.",
            code="INVALID_IDEMPOTENCY_KEY",
            details=[{"field": "Idempotency-Key", "issue": "must be a UUID"}],
        ) from exc


IdempotencyKey = Annotated[uuid.UUID | None, Depends(get_idempotency_key)]


def get_client_ip(request: Request) -> str:
    """Best-effort client address for the login rate limiter.

    ``X-Forwarded-For`` is trusted because this API only ever runs behind its own
    nginx. If it is ever exposed directly, this becomes attacker-controlled.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
