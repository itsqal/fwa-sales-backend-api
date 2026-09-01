"""Admin auth endpoints for the supply chain web dashboard."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.deps import (
    AppSettings,
    CurrentAdmin,
    DbSession,
    get_admin_session_id,
    get_client_ip,
)
from app.core.errors import TooManyRequestsError
from app.core.ratelimit import FixedWindowRateLimiter
from app.schemas.admin import (
    AdminAuthTokens,
    AdminLoginRequest,
    AdminProfileOut,
    AdminRefreshRequest,
)
from app.services import admin_auth as admin_auth_service

router = APIRouter()

# Module-level so the counter survives between requests, and separate from the AE
# limiter so a brute-force run against the dashboard cannot exhaust the field force's
# allowance (or vice versa).
_login_limiter: FixedWindowRateLimiter | None = None


def _limiter(attempts: int, window_seconds: int) -> FixedWindowRateLimiter:
    global _login_limiter
    if _login_limiter is None:
        _login_limiter = FixedWindowRateLimiter(
            max_attempts=attempts, window_seconds=window_seconds
        )
    return _login_limiter


@router.post(
    "/auth/login",
    tags=["Admin Auth"],
    summary="Log in to the web dashboard",
    response_model=AdminAuthTokens,
    responses={401: {"description": "Unauthenticated"}, 429: {"description": "Rate limited"}},
)
async def admin_login(
    request: Request,
    payload: AdminLoginRequest,
    session: DbSession,
    settings: AppSettings,
) -> AdminAuthTokens:
    """Usernames are matched case-insensitively, exactly as AE codes are.

    Accounts are seeded out-of-band by IOH HQ; there is no self-service registration.
    """
    limiter = _limiter(settings.login_rate_limit_attempts, settings.login_rate_limit_window_seconds)
    # Keyed on username *and* address, for the same reason as the AE limiter: one admin
    # mistyping a password must not lock out a colleague behind the same office NAT.
    key = f"admin|{payload.username.upper()}|{get_client_ip(request)}"

    retry_after = limiter.check(key)
    if retry_after is not None:
        raise TooManyRequestsError(
            "Too many sign-in attempts. Wait a moment and try again.",
            code="TOO_MANY_LOGIN_ATTEMPTS",
            headers={"Retry-After": str(retry_after)},
        )

    tokens = await admin_auth_service.authenticate(
        session,
        settings,
        username=payload.username,
        password=payload.password,
        device_label=payload.device_label,
    )
    limiter.reset(key)
    return tokens


@router.post(
    "/auth/refresh",
    tags=["Admin Auth"],
    summary="Exchange an admin refresh token for a new access token",
    response_model=AdminAuthTokens,
    responses={401: {"description": "Unauthenticated"}},
)
async def admin_refresh_token(
    payload: AdminRefreshRequest, session: DbSession, settings: AppSettings
) -> AdminAuthTokens:
    return await admin_auth_service.refresh(session, settings, refresh_token=payload.refresh_token)


@router.post(
    "/auth/logout",
    tags=["Admin Auth"],
    summary="Revoke the current admin refresh token",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"description": "Unauthenticated"}},
)
async def admin_logout(
    session: DbSession,
    _: CurrentAdmin,
    session_id: Annotated[uuid.UUID, Depends(get_admin_session_id)],
) -> Response:
    await admin_auth_service.logout(session, session_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me",
    tags=["Admin Auth"],
    summary="Profile and organisation binding of the signed-in admin",
    response_model=AdminProfileOut,
    responses={401: {"description": "Unauthenticated"}},
)
async def get_admin_me(admin: CurrentAdmin) -> AdminProfileOut:
    """The dashboard builds its sidebar from this: `role` selects the menu set and
    `organisation` supplies the topbar label."""
    return admin_auth_service.to_profile(admin)
