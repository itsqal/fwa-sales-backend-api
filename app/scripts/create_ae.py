"""Create a real Account Executive account.

``seed.py`` exists for development and deliberately creates fixture AEs with a password
printed in the source. Production needs a different tool, and there is no admin API to
do it with — back-office administration is a separate future concern, so this is a CLI
rather than an endpoint.

    docker compose -f docker-compose.prod.yml exec api \
        python -m app.scripts.create_ae \
        --ae-code AE-BENGKULU1 \
        --full-name "Hendra Jaya" \
        --region-code BENGKULU --region-name Bengkulu \
        --mpx-code MPX-BKL-01

Omit ``--password`` and one is generated and printed once. It is never stored in
plaintext and cannot be recovered afterwards — hand it to the AE and move on.
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
from app.models.enums import AeStatus
from app.models.identity import AccountExecutive, Region


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create an Account Executive login.")
    parser.add_argument("--ae-code", required=True, help="HQ-issued code, e.g. AE-BENGKULU1")
    parser.add_argument("--full-name", required=True)
    parser.add_argument("--region-code", required=True, help="e.g. BENGKULU")
    parser.add_argument("--region-name", help="Defaults to a title-cased region code.")
    parser.add_argument("--province")
    parser.add_argument("--mpx-code", help="Stock point the AE draws from. Reference only.")
    parser.add_argument("--phone-number")
    parser.add_argument("--email")
    parser.add_argument(
        "--password",
        help="Leave unset to generate one. It is printed once and never stored in the clear.",
    )
    parser.add_argument(
        "--must-change-password",
        action="store_true",
        help=(
            "Force a password reset at first login. WARNING: the API currently exposes "
            "no password-change endpoint, so an AE flagged this way is told to change "
            "their password with no way to do it. Leave off until that endpoint exists."
        ),
    )
    return parser


async def create(session: AsyncSession, args: argparse.Namespace) -> str:
    # Login is case-insensitive, so the duplicate check has to be too — otherwise this
    # script would happily create AE-BENGKULU1 alongside ae-bengkulu1 and the unique
    # index on upper(ae_code) would reject it with a much less helpful message.
    existing = await session.scalar(
        select(AccountExecutive).where(func.upper(AccountExecutive.ae_code) == args.ae_code.upper())
    )
    if existing is not None:
        raise SystemExit(f"An AE with code {args.ae_code} already exists. Nothing was changed.")

    region = await session.scalar(
        select(Region).where(Region.region_code == args.region_code.upper())
    )
    if region is None:
        region = Region(
            region_code=args.region_code.upper(),
            region_name=args.region_name or args.region_code.title(),
            province=args.province,
        )
        session.add(region)
        await session.flush()

    password: str = args.password or secrets.token_urlsafe(12)

    session.add(
        AccountExecutive(
            ae_code=args.ae_code,
            full_name=args.full_name,
            password_hash=hash_password(password),
            phone_number=args.phone_number,
            email=args.email,
            region_id=region.region_id,
            mpx_code=args.mpx_code,
            status=AeStatus.ACTIVE,
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

    print(f"Created {args.ae_code} ({args.full_name}).")
    if args.password:
        print("Password: the one you supplied.")
    else:
        print(f"Password: {password}")
        print("Copy it now — it is stored only as an argon2id hash and cannot be recovered.")
    if args.must_change_password:
        print(
            "\nWARNING: must_change_pw is set, but no password-change endpoint exists yet. "
            "This AE will be prompted to reset with no way to complete it.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    asyncio.run(main())
