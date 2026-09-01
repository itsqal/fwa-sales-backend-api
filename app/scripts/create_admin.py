"""Create a web dashboard administrator account.

There is no admin user-management screen in v1 and no registration endpoint — accounts
are seeded out-of-band by IOH HQ, exactly as AE accounts are. This is that tool.

    docker compose -f docker-compose.prod.yml exec api \
        python -m app.scripts.create_admin \
        --username dp.advan \
        --full-name "Atha Marcella" \
        --role DP_ADMIN --org-code ADVAN

The organisation binding is decided by the role and validated here before the database
sees it, so a mistake produces a readable message rather than a CHECK violation:

    DP_ADMIN   --org-code is a device_partner.code   (ADVAN, RABIT, ZTE, HKM, ...)
    MPX_ADMIN  --org-code is an mpx.code             (MPX-BKL-01, ...)
    IOH_ADMIN  --org-code must be omitted            (global-read by design)

Omit ``--password`` and one is generated and printed once. It is never stored in
plaintext and cannot be recovered afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.session import dispose_engine, get_sessionmaker
from app.models.admin import AdminUser
from app.models.enums import AdminRole, AdminStatus
from app.models.organisation import DevicePartner, Mpx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a web dashboard admin login.")
    parser.add_argument("--username", required=True, help="Matched case-insensitively at login.")
    parser.add_argument("--full-name", required=True, help="Rendered into every attestation.")
    parser.add_argument(
        "--role",
        required=True,
        choices=[role.value for role in AdminRole],
    )
    parser.add_argument(
        "--org-code",
        help="device_partner.code for DP_ADMIN, mpx.code for MPX_ADMIN, omitted for IOH_ADMIN.",
    )
    parser.add_argument("--email")
    parser.add_argument(
        "--password",
        help="Leave unset to generate one. It is printed once and never stored in the clear.",
    )
    parser.add_argument(
        "--must-change-password",
        action="store_true",
        help=(
            "Force a password reset at first login. WARNING: no password-change endpoint "
            "exists for admins yet, so an account flagged this way is told to change its "
            "password with no way to do it. Leave off until that endpoint exists."
        ),
    )
    return parser


async def _resolve_binding(
    session: AsyncSession, *, role: AdminRole, org_code: str | None
) -> tuple[str | None, str | None]:
    """Return ``(device_partner_id, mpx_id)`` for the role, or fail with a clear message.

    This mirrors ``ck_admin_org_binding``. The constraint is the real guarantee; this is
    only here so a typo produces "no Device Partner with code ADVN" rather than a raw
    integrity error.
    """
    if role is AdminRole.IOH_ADMIN:
        if org_code:
            raise SystemExit(
                "IOH_ADMIN is global-read and binds to no organisation. Drop --org-code."
            )
        return None, None

    if not org_code:
        raise SystemExit(f"{role.value} requires --org-code. Nothing was changed.")

    if role is AdminRole.DP_ADMIN:
        partner = await session.scalar(
            select(DevicePartner).where(DevicePartner.code == org_code.upper())
        )
        if partner is None:
            raise SystemExit(f"No Device Partner with code {org_code}. Nothing was changed.")
        return str(partner.device_partner_id), None

    mpx = await session.scalar(select(Mpx).where(Mpx.code == org_code.upper()))
    if mpx is None:
        raise SystemExit(f"No MPX with code {org_code}. Nothing was changed.")
    return None, str(mpx.mpx_id)


async def create(session: AsyncSession, args: argparse.Namespace) -> str:
    # Login is case-insensitive, so the duplicate check has to be too — otherwise this
    # script would try to create dp.advan alongside DP.ADVAN and the unique index on
    # upper(username) would reject it with a far less helpful message.
    existing = await session.scalar(
        select(AdminUser).where(func.upper(AdminUser.username) == args.username.upper())
    )
    if existing is not None:
        raise SystemExit(f"An admin named {args.username} already exists. Nothing was changed.")

    role = AdminRole(args.role)
    device_partner_id, mpx_id = await _resolve_binding(session, role=role, org_code=args.org_code)

    password: str = args.password or secrets.token_urlsafe(12)

    session.add(
        AdminUser(
            username=args.username,
            email=args.email,
            full_name=args.full_name,
            password_hash=hash_password(password),
            role=role,
            device_partner_id=device_partner_id,
            mpx_id=mpx_id,
            status=AdminStatus.ACTIVE,
            must_change_pw=bool(args.must_change_password),
        )
    )
    await session.flush()
    return password


async def main() -> None:
    args = build_parser().parse_args()

    async with get_sessionmaker()() as session:
        password = await create(session, args)
        await session.commit()
    await dispose_engine()

    binding = args.org_code or "none (global read)"
    print(f"Created {args.username} ({args.full_name}) as {args.role}, bound to {binding}.")
    if args.password:
        print("Password: the one you supplied.")
    else:
        print(f"Password: {password}")
        print("Copy it now — it is stored only as an argon2id hash and cannot be recovered.")
    if args.must_change_password:
        print(
            "\nWARNING: must_change_pw is set, but no admin password-change endpoint exists "
            "yet. This account will be prompted to reset with no way to complete it.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    asyncio.run(main())
