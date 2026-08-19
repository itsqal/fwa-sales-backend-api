"""Auth endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.deps import AppSettings, CurrentAe, DbSession, get_client_ip, get_session_id
from app.core.errors import TooManyRequestsError
from app.core.ratelimit import FixedWindowRateLimiter
from app.schemas.auth import AccountExecutiveOut, AuthTokens, LoginRequest, RefreshRequest
from app.services import auth as auth_service

router = APIRouter()

# Module-level so the counter survives between requests. See ratelimit.py on why a
# per-process limiter is the right call for this deployment.
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
    tags=["Auth"],
    summary="Log in with AE code and password",
    response_model=AuthTokens,
    responses={401: {"description": "Unauthenticated"}, 429: {"description": "Rate limited"}},
)
async def login(
    request: Request,
    payload: LoginRequest,
    session: DbSession,
    settings: AppSettings,
) -> AuthTokens:
    """AE codes are matched case-insensitively — `AE-SIDOARJO1` and `ae-sidoarjo1` are
    the same account."""
    limiter = _limiter(settings.login_rate_limit_attempts, settings.login_rate_limit_window_seconds)
    # Keyed on code *and* address: one AE fat-fingering their password must not lock
    # out a colleague on the same office wifi, and one address must not be able to
    # spray every AE code in a region.
    key = f"{payload.ae_code.upper()}|{get_client_ip(request)}"

    retry_after = limiter.check(key)
    if retry_after is not None:
        raise TooManyRequestsError(
            "Too many sign-in attempts. Wait a moment and try again.",
            code="TOO_MANY_LOGIN_ATTEMPTS",
            headers={"Retry-After": str(retry_after)},
        )

    tokens = await auth_service.authenticate(
        session,
        settings,
        ae_code=payload.ae_code,
        password=payload.password,
        device_label=payload.device_label,
    )
    limiter.reset(key)
    return tokens


@router.post(
    "/auth/refresh",
    tags=["Auth"],
    summary="Exchange a refresh token for a new access token",
    response_model=AuthTokens,
    responses={401: {"description": "Unauthenticated"}},
)
async def refresh_token(
    payload: RefreshRequest, session: DbSession, settings: AppSettings
) -> AuthTokens:
    return await auth_service.refresh(session, settings, refresh_token=payload.refresh_token)


@router.post(
    "/auth/logout",
    tags=["Auth"],
    summary="Revoke the current refresh token",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={401: {"description": "Unauthenticated"}},
)
async def logout(
    session: DbSession,
    _: CurrentAe,
    session_id: Annotated[uuid.UUID, Depends(get_session_id)],
) -> Response:
    await auth_service.logout(session, session_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me",
    tags=["Auth"],
    summary="Profile of the signed-in AE",
    response_model=AccountExecutiveOut,
    responses={401: {"description": "Unauthenticated"}},
)
async def get_me(ae: CurrentAe) -> AccountExecutiveOut:
    """Supplies the read-only "ID AE" field shown on every input form."""
    return auth_service.to_profile(ae)
