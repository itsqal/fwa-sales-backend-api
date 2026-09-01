"""Web dashboard login, refresh-token rotation, logout, and the profile projection.

A deliberate near-duplicate of :mod:`app.services.auth`. The two were kept separate
rather than generalised behind a shared helper because the AE path is deployed and in
the field: a refactor that touched it to accommodate the dashboard would put the mobile
app's authentication at risk for the sake of removing about forty lines.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import UnauthorizedError
from app.core.security import (
    create_admin_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    needs_rehash,
    verify_password,
)
from app.models.admin import AdminRefreshToken, AdminUser
from app.models.enums import AdminStatus
from app.schemas.admin import AdminAuthTokens, AdminOrganisationOut, AdminProfileOut

# Generic on purpose, exactly as the AE message is: distinguishing "no such user" from
# "wrong password" turns the login form into an account enumerator.
_BAD_CREDENTIALS = "Username or password is incorrect."


def to_profile(admin: AdminUser) -> AdminProfileOut:
    """Project an admin onto the payload the dashboard builds its sidebar from.

    The organisation binding is read from whichever relationship the role permits.
    ``ck_admin_org_binding`` guarantees at most one of them is set, so this cannot
    silently pick the wrong one.
    """
    organisation: AdminOrganisationOut | None = None
    if admin.device_partner is not None:
        organisation = AdminOrganisationOut(
            type="DEVICE_PARTNER",
            id=admin.device_partner.device_partner_id,
            code=admin.device_partner.code,
            name=admin.device_partner.name,
        )
    elif admin.mpx is not None:
        organisation = AdminOrganisationOut(
            type="MPX",
            id=admin.mpx.mpx_id,
            code=admin.mpx.code,
            name=admin.mpx.name,
            legal_name=admin.mpx.legal_name,
            circle=admin.mpx.circle,
        )

    return AdminProfileOut(
        admin_user_id=admin.admin_user_id,
        username=admin.username,
        email=admin.email,
        full_name=admin.full_name,
        role=admin.role,
        organisation=organisation,
        status=admin.status,
    )


async def authenticate(
    session: AsyncSession,
    settings: Settings,
    *,
    username: str,
    password: str,
    device_label: str | None,
) -> AdminAuthTokens:
    """Verify credentials and mint a session.

    The username is matched case-insensitively, backed by ``uq_admin_username_ci`` so
    the comparison uses an index rather than scanning.
    """
    admin = await session.scalar(
        select(AdminUser).where(func.upper(AdminUser.username) == username.upper())
    )

    if admin is None or not verify_password(password, admin.password_hash):
        raise UnauthorizedError(_BAD_CREDENTIALS, code="INVALID_CREDENTIALS")

    if admin.status is not AdminStatus.ACTIVE:
        raise UnauthorizedError(
            "This account is not active. Contact your administrator.",
            code="ACCOUNT_INACTIVE",
        )

    # Transparently upgrade a hash whose argon2 parameters have since been raised.
    if needs_rehash(admin.password_hash):
        admin.password_hash = hash_password(password)

    return await _issue_session(session, settings, admin=admin, device_label=device_label)


async def refresh(
    session: AsyncSession, settings: Settings, *, refresh_token: str
) -> AdminAuthTokens:
    """Rotate a refresh token. The presented token is revoked whether or not it is reused."""
    token_row = await session.scalar(
        select(AdminRefreshToken).where(
            AdminRefreshToken.token_hash == hash_refresh_token(refresh_token)
        )
    )

    now = datetime.now(UTC)
    if token_row is None or token_row.revoked_at is not None or token_row.expires_at <= now:
        raise UnauthorizedError(
            "Your session has expired. Sign in again.", code="INVALID_REFRESH_TOKEN"
        )

    admin = await session.get(AdminUser, token_row.admin_user_id)
    if admin is None or admin.status is not AdminStatus.ACTIVE:
        raise UnauthorizedError(
            "This account is not active. Contact your administrator.", code="ACCOUNT_INACTIVE"
        )

    token_row.revoked_at = now
    return await _issue_session(session, settings, admin=admin, device_label=token_row.device_label)


async def logout(session: AsyncSession, *, session_id: uuid.UUID) -> None:
    """Revoke the calling browser's session only, not every session the admin holds."""
    await session.execute(
        update(AdminRefreshToken)
        .where(AdminRefreshToken.token_id == session_id, AdminRefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def _issue_session(
    session: AsyncSession,
    settings: Settings,
    *,
    admin: AdminUser,
    device_label: str | None,
) -> AdminAuthTokens:
    raw_refresh = generate_refresh_token()
    token_row = AdminRefreshToken(
        admin_user_id=admin.admin_user_id,
        token_hash=hash_refresh_token(raw_refresh),
        device_label=device_label,
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
    )
    session.add(token_row)
    await session.flush()

    access_token, expires_in = create_admin_access_token(
        settings, admin_user_id=admin.admin_user_id, session_id=token_row.token_id
    )
    return AdminAuthTokens(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=expires_in,
        must_change_password=admin.must_change_pw,
        profile=to_profile(admin),
    )
