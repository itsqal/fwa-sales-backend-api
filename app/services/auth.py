"""Login, refresh-token rotation, logout, and the profile projection."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import UnauthorizedError
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    needs_rehash,
    verify_password,
)
from app.models.enums import AeStatus
from app.models.identity import AccountExecutive, AuthRefreshToken
from app.schemas.auth import AccountExecutiveOut, AuthTokens, RegionOut, WorkShiftOut

# A deliberately generic message. Distinguishing "no such AE code" from "wrong password"
# would turn the login form into an account enumerator, and AE codes are guessable.
_BAD_CREDENTIALS = "AE code or password is incorrect."


def to_profile(ae: AccountExecutive) -> AccountExecutiveOut:
    return AccountExecutiveOut(
        ae_id=ae.ae_id,
        ae_code=ae.ae_code,
        full_name=ae.full_name,
        region=(
            RegionOut(region_code=ae.region.region_code, region_name=ae.region.region_name)
            if ae.region is not None
            else None
        ),
        mpx_code=ae.mpx_code,
        work_shift=WorkShiftOut(
            start=ae.work_shift_start.strftime("%H:%M"),
            end=ae.work_shift_end.strftime("%H:%M"),
        ),
        status=ae.status,
    )


async def authenticate(
    session: AsyncSession,
    settings: Settings,
    *,
    ae_code: str,
    password: str,
    device_label: str | None,
) -> AuthTokens:
    """Verify credentials and mint a session.

    The AE code is matched case-insensitively, backed by the functional unique index
    ``uq_ae_code_ci`` so the comparison uses an index rather than scanning.
    """
    ae = await session.scalar(
        select(AccountExecutive).where(func.upper(AccountExecutive.ae_code) == ae_code.upper())
    )

    if ae is None or not verify_password(password, ae.password_hash):
        raise UnauthorizedError(_BAD_CREDENTIALS, code="INVALID_CREDENTIALS")

    if ae.status is not AeStatus.ACTIVE:
        raise UnauthorizedError(
            "This account is not active. Contact your supervisor.",
            code="ACCOUNT_INACTIVE",
        )

    # Transparently upgrade a hash whose argon2 parameters have since been raised.
    if needs_rehash(ae.password_hash):
        ae.password_hash = hash_password(password)

    return await _issue_session(session, settings, ae=ae, device_label=device_label)


async def refresh(session: AsyncSession, settings: Settings, *, refresh_token: str) -> AuthTokens:
    """Rotate a refresh token. The presented token is revoked whether or not it is reused."""
    token_row = await session.scalar(
        select(AuthRefreshToken).where(
            AuthRefreshToken.token_hash == hash_refresh_token(refresh_token)
        )
    )

    now = datetime.now(UTC)
    if token_row is None or token_row.revoked_at is not None or token_row.expires_at <= now:
        raise UnauthorizedError(
            "Your session has expired. Sign in again.", code="INVALID_REFRESH_TOKEN"
        )

    ae = await session.get(AccountExecutive, token_row.ae_id)
    if ae is None or ae.status is not AeStatus.ACTIVE:
        raise UnauthorizedError(
            "This account is not active. Contact your supervisor.", code="ACCOUNT_INACTIVE"
        )

    token_row.revoked_at = now
    return await _issue_session(session, settings, ae=ae, device_label=token_row.device_label)


async def logout(session: AsyncSession, *, session_id: uuid.UUID) -> None:
    """Revoke the calling device's session only, not every session the AE holds."""
    await session.execute(
        update(AuthRefreshToken)
        .where(AuthRefreshToken.token_id == session_id, AuthRefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def _issue_session(
    session: AsyncSession,
    settings: Settings,
    *,
    ae: AccountExecutive,
    device_label: str | None,
) -> AuthTokens:
    raw_refresh = generate_refresh_token()
    token_row = AuthRefreshToken(
        ae_id=ae.ae_id,
        token_hash=hash_refresh_token(raw_refresh),
        device_label=device_label,
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
    )
    session.add(token_row)
    await session.flush()

    access_token, expires_in = create_access_token(
        settings, ae_id=ae.ae_id, session_id=token_row.token_id
    )
    return AuthTokens(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=expires_in,
        must_change_password=ae.must_change_pw,
        profile=to_profile(ae),
    )
