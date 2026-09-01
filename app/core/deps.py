"""Shared FastAPI dependencies.

The important one is :func:`get_current_ae`. AE identity comes from the JWT and from
nowhere else — no endpoint accepts an ``aeId`` in a body or query string, so a client
has no way to write on another AE's behalf.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

import jwt
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, UnauthorizedError, ValidationFailedError
from app.core.security import (
    constant_time_equals,
    decode_access_token,
    decode_admin_access_token,
)
from app.db.session import session_scope
from app.models.admin import AdminRefreshToken, AdminUser
from app.models.enums import AdminRole, AdminStatus, AeStatus
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


async def get_current_admin(
    session: DbSession,
    settings: AppSettings,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AdminUser:
    """The web dashboard principal, resolved from an admin-audience token.

    The AE counterpart of this function is :func:`get_current_ae`, and the two are
    mutually exclusive by construction: this one demands ``aud="admin"``, that one
    demands its absence. Neither can authenticate the other's caller, which is what
    lets golden rule 2 keep holding literally on AE routes while admin routes carry
    an organisation binding instead.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError(
            "Sign in to continue.",
            code="MISSING_TOKEN",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_admin_access_token(settings, credentials.credentials)
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError(
            "Your session has expired. Sign in again.",
            code="TOKEN_EXPIRED",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.PyJWTError as exc:
        # Also the path an AE token takes: no `aud` claim, so PyJWT raises
        # MissingRequiredClaimError here rather than authenticating a mobile user
        # against the dashboard.
        raise UnauthorizedError(
            "Your session is no longer valid. Sign in again.",
            code="INVALID_TOKEN",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        admin_user_id = uuid.UUID(str(payload["sub"]))
        session_id = uuid.UUID(str(payload["sid"]))
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("Your session is no longer valid. Sign in again.") from exc

    admin = await session.get(AdminUser, admin_user_id)
    if admin is None:
        raise UnauthorizedError("Your session is no longer valid. Sign in again.")

    if admin.status is not AdminStatus.ACTIVE:
        raise UnauthorizedError(
            "This account is not active. Contact your administrator.",
            code="ACCOUNT_INACTIVE",
        )

    # A revoked session must stop working before its access token expires, otherwise
    # "log out everywhere" is only advisory for up to 15 minutes.
    revoked = await session.scalar(
        select(AdminRefreshToken.token_id).where(
            AdminRefreshToken.token_id == session_id,
            AdminRefreshToken.revoked_at.is_(None),
        )
    )
    if revoked is None:
        raise UnauthorizedError(
            "Your session has ended. Sign in again.",
            code="SESSION_REVOKED",
        )

    return admin


CurrentAdmin = Annotated[AdminUser, Depends(get_current_admin)]


def require_roles(*roles: AdminRole) -> Callable[[AdminUser], Awaitable[AdminUser]]:
    """Restrict a route to specific admin roles.

    Role is *coarse* authorisation — which screens exist. It is not a substitute for
    org scoping, which is finer and belongs in the service layer: `require_roles`
    answers "may an MPX admin allocate stock at all", never "whose stock".

    A 403 is correct here, unlike the AE convention of returning 404 for another AE's
    record. Nothing is leaked by telling a Device Partner that MPX screens exist — the
    dashboard's own sidebar already does.
    """

    async def dependency(admin: CurrentAdmin) -> AdminUser:
        if admin.role not in roles:
            raise ForbiddenError(
                "Your role does not have access to this action.",
                code="ROLE_NOT_PERMITTED",
            )
        return admin

    return dependency


def get_admin_session_id(
    settings: AppSettings,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> uuid.UUID:
    """The refresh-token id embedded in an admin access token — the caller's session."""
    if credentials is None:
        raise UnauthorizedError("Sign in to continue.", code="MISSING_TOKEN")
    try:
        payload = decode_admin_access_token(settings, credentials.credentials)
        return uuid.UUID(str(payload["sid"]))
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise UnauthorizedError("Your session is no longer valid.") from exc


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
