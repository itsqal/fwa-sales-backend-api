"""Password hashing, JWT access tokens, and refresh-token material.

Refresh tokens are opaque random strings. Only their SHA-256 digest is stored, so a
database leak does not hand an attacker usable sessions. Each access token carries the
id of the refresh token that minted it (``sid``), which is what lets ``POST /auth/logout``
revoke exactly the calling device's session rather than every session the AE has.

Two audiences share this module. AE tokens carry **no** ``aud`` claim and are left
exactly as they were — the mobile app is in the field and its auth path is not being
modified by the supply-chain work. Admin tokens carry ``aud="admin"``. That asymmetry
is what isolates the two, and it works in both directions for free: PyJWT raises
``InvalidAudienceError`` when a token carries an audience the decoder did not ask for,
and ``MissingRequiredClaimError`` when the decoder demands one the token lacks. Both
are ``PyJWTError`` subclasses, so each side's existing handler already turns them into
a 401 rather than leaking a 500.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings

_hasher: Final = PasswordHasher()

ACCESS_TOKEN_TYPE: Final = "access"

# The audience claim that separates the web dashboard from the mobile app. Changing
# this string invalidates every live admin session.
ADMIN_AUDIENCE: Final = "admin"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def create_access_token(
    settings: Settings,
    *,
    ae_id: UUID,
    session_id: UUID,
) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)``."""
    now = datetime.now(UTC)
    expires_in = settings.access_token_ttl_seconds
    payload: dict[str, Any] = {
        "sub": str(ae_id),
        "sid": str(session_id),
        "typ": ACCESS_TOKEN_TYPE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


def decode_access_token(settings: Settings, token: str) -> dict[str, Any]:
    """Decode and validate an access token. Raises ``jwt.PyJWTError`` on any problem."""
    payload: dict[str, Any] = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        options={"require": ["exp", "sub", "sid"]},
    )
    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not an access token")
    return payload


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def create_admin_access_token(
    settings: Settings,
    *,
    admin_user_id: UUID,
    session_id: UUID,
) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)`` for a web dashboard principal.

    Identical to :func:`create_access_token` apart from the ``aud`` claim, which is the
    whole point: an admin token must not authenticate an AE route even though both are
    signed with the same secret.
    """
    now = datetime.now(UTC)
    expires_in = settings.access_token_ttl_seconds
    payload: dict[str, Any] = {
        "sub": str(admin_user_id),
        "sid": str(session_id),
        "typ": ACCESS_TOKEN_TYPE,
        "aud": ADMIN_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


def decode_admin_access_token(settings: Settings, token: str) -> dict[str, Any]:
    """Decode and validate an admin access token. Raises ``jwt.PyJWTError`` on any problem.

    ``aud`` is in the ``require`` list as well as being verified, so an AE token — which
    carries no audience at all — is rejected here rather than falling through to a
    lookup that would miss anyway.
    """
    payload: dict[str, Any] = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        audience=ADMIN_AUDIENCE,
        options={"require": ["exp", "sub", "sid", "aud"]},
    )
    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        raise jwt.InvalidTokenError("not an access token")
    return payload
