"""Supply chain step 1 — organisations and the admin principal.

First migration of the supply-chain extension (Supply Chain Backend Extension Spec.md
§7 step 1). New tables only, no existing table touched, so this cannot affect the AE
mobile app. Hand-written for the same reason as 0001 and 0002: autogenerate does not
see CHECK constraints, functional indexes, or partial indexes, and this migration's
correctness lives almost entirely in those.

Three deliberate departures from the DDL printed in the spec, each to match what is
already in this repo rather than what the spec drafted in isolation:

* **UUID primary keys, not BIGINT.** Every entity the API references by URL uses
  ``UUID DEFAULT gen_random_uuid()`` here (docs/schema.sql, "Conventions").

* **VARCHAR + CHECK for ``role``, not ``CREATE TYPE admin_role AS ENUM``.** Same reason
  the AE schema avoided native enums: a value can then be added in a plain transaction.
  The spec itself relies on that property later, when it widens ``fwa_inventory.status``.

* **A functional unique index on ``upper(username)``, not CITEXT.** Case-insensitive
  login already has a pattern in this schema — ``uq_ae_code_ci`` — and reusing it
  avoids adding an extension for one column.

``ck_admin_org_binding`` is the load-bearing constraint in this file and is transcribed
from the spec unchanged. An admin principal carries exactly one organisation binding, as
wide as its role allows and no wider, enforced by PostgreSQL rather than by service
code: an MPX admin who somehow acquired a ``device_partner_id`` could read every
competitor's order book.

Seed data. The six Device Partners and the two MPXs are inserted here rather than in
``app/scripts/seed.py`` because they are production reference data, not dev fixtures —
no admin account can exist without an organisation to bind to. Two values are inferred
and should be corrected by UPDATE once confirmed, never by editing this file:
``device_partner.name`` currently repeats the code (no legal names were supplied), and
``mpx.circle`` is derived from each MPX's region. The MPX codes match the format already
live in the AE seed (MPX-BKL-01, MPX-SDA-02) — this migration ratifies that format
rather than changing it, so no backfill of ``account_executive.mpx_code`` is required.

Revision ID: 0003_supply_org_and_admin_auth
Note: alembic_version.version_num is VARCHAR(32); revision ids must fit.
Revises: 0002_device_network_generation
Create Date: 2026-09-01

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_supply_org_and_admin_auth"
down_revision: str | None = "0002_device_network_generation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -----------------------------------------------------------------
    # MODULE E — Organisations
    # -----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE device_partner (
            device_partner_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code              VARCHAR(20)  NOT NULL,
            name              VARCHAR(150) NOT NULL,
            status            VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_device_partner_code   UNIQUE (code),
            CONSTRAINT ck_device_partner_code   CHECK (code ~ '^[A-Z0-9]{2,20}$'),
            CONSTRAINT ck_device_partner_status CHECK (status IN ('ACTIVE','INACTIVE'))
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE device_partner IS "
        "'Hardware supplier that requests MSISDNs from IOH, pairs them to IMEIs, and "
        "fulfils MPX device orders.'"
    )
    op.execute(
        "COMMENT ON COLUMN device_partner.code IS "
        "'Prefix of every MSISDN PO number this partner raises: ADVAN-20250523-207.'"
    )

    op.execute(
        """
        CREATE TABLE mpx (
            mpx_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code         VARCHAR(20)  NOT NULL,
            name         VARCHAR(150) NOT NULL,
            legal_name   VARCHAR(200),
            circle       VARCHAR(20),
            region_id    UUID REFERENCES region(region_id),
            external_ref VARCHAR(200),
            status       VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_mpx_code   UNIQUE (code),
            CONSTRAINT ck_mpx_code   CHECK (code ~ '^MPX-[A-Z]{2,4}-[0-9]{2}$'),
            CONSTRAINT ck_mpx_circle CHECK
                (circle IS NULL OR circle IN ('JAVA','KALISUMAPA','SUMATERA')),
            CONSTRAINT ck_mpx_status CHECK (status IN ('ACTIVE','INACTIVE'))
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE mpx IS "
        "'Distributor / stock point. Orders devices from a Device Partner, confirms "
        "receipt, and allocates units to Account Executives.'"
    )
    op.execute(
        "COMMENT ON COLUMN mpx.code IS "
        "'Canonical identifier, format MPX-<AREA>-<NN>. Joined to "
        "account_executive.mpx_code, which already carries this format. The mockups "
        "showed three incompatible legacy formats in this column; those belong in "
        "external_ref, never in a primary business identifier.'"
    )
    op.execute(
        "COMMENT ON COLUMN mpx.external_ref IS "
        "'Identifier as it appears in an upstream source system. Display and search use "
        "code and name; this column exists so a legacy string never has to be parsed.'"
    )

    # -----------------------------------------------------------------
    # Admin principals — a separate table and token audience from the AE.
    # -----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE admin_user (
            admin_user_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            username          VARCHAR(50)  NOT NULL,
            email             VARCHAR(150),
            password_hash     TEXT         NOT NULL,
            full_name         VARCHAR(150) NOT NULL,
            role              VARCHAR(20)  NOT NULL,
            device_partner_id UUID REFERENCES device_partner(device_partner_id),
            mpx_id            UUID REFERENCES mpx(mpx_id),
            must_change_pw    BOOLEAN      NOT NULL DEFAULT TRUE,
            status            VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT ck_admin_role   CHECK (role IN ('DP_ADMIN','IOH_ADMIN','MPX_ADMIN')),
            CONSTRAINT ck_admin_status CHECK (status IN ('ACTIVE','SUSPENDED','INACTIVE')),
            CONSTRAINT ck_admin_org_binding CHECK (
                (role = 'DP_ADMIN'  AND device_partner_id IS NOT NULL AND mpx_id IS NULL) OR
                (role = 'MPX_ADMIN' AND mpx_id IS NOT NULL AND device_partner_id IS NULL) OR
                (role = 'IOH_ADMIN' AND device_partner_id IS NULL AND mpx_id IS NULL)
            )
        )
        """
    )
    # Login is case-insensitive, as it is for an AE code. Same pattern as uq_ae_code_ci.
    op.execute("CREATE UNIQUE INDEX uq_admin_username_ci ON admin_user (upper(username))")
    op.execute(
        "CREATE UNIQUE INDEX uq_admin_email_ci ON admin_user (upper(email)) WHERE email IS NOT NULL"
    )
    op.execute(
        "COMMENT ON TABLE admin_user IS "
        "'Web dashboard principal. Deliberately separate from account_executive: admin "
        "tokens carry aud=admin and are rejected by AE routes, and AE tokens are "
        "rejected by /admin routes, so the deployed mobile app auth path is not "
        "modified by this programme at all.'"
    )
    op.execute(
        "COMMENT ON CONSTRAINT ck_admin_org_binding ON admin_user IS "
        "'The binding is exactly as wide as the role allows. Enforced in the database "
        "rather than in Python because this is the constraint that separates three "
        "competing companies reading one dataset.'"
    )

    op.execute(
        """
        CREATE TABLE admin_refresh_token (
            token_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            admin_user_id UUID NOT NULL
                            REFERENCES admin_user(admin_user_id) ON DELETE CASCADE,
            token_hash    TEXT        NOT NULL,
            device_label  VARCHAR(120),
            issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at    TIMESTAMPTZ NOT NULL,
            revoked_at    TIMESTAMPTZ,
            CONSTRAINT uq_admin_refresh_token_hash UNIQUE (token_hash)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_admin_refresh_active ON admin_refresh_token (admin_user_id) "
        "WHERE revoked_at IS NULL"
    )
    op.execute(
        "COMMENT ON TABLE admin_refresh_token IS "
        "'Mirrors auth_refresh_token exactly — one row per browser session, only the "
        "digest stored, revocable per device.'"
    )

    # -----------------------------------------------------------------
    # Reference data. Guarded so a downgrade/re-upgrade cycle cannot duplicate rows.
    # -----------------------------------------------------------------
    op.execute(
        """
        INSERT INTO device_partner (code, name)
        SELECT v.code, v.name
        FROM (VALUES
            ('ADVAN',  'ADVAN'),
            ('RABIT',  'RABIT'),
            ('ZTE',    'ZTE'),
            ('HKM',    'HKM'),
            ('HUAWEI', 'HUAWEI'),
            ('BANGGA', 'BANGGA')
        ) AS v(code, name)
        WHERE NOT EXISTS (SELECT 1 FROM device_partner d WHERE d.code = v.code)
        """
    )

    # region_id is resolved by code so this works against any environment whose regions
    # were seeded independently. A missing region leaves the MPX unregioned rather than
    # failing the migration — the FK is nullable and nothing in step 1 reads it yet.
    op.execute(
        """
        INSERT INTO mpx (code, name, legal_name, circle, region_id)
        SELECT v.code, v.name, v.legal_name, v.circle, r.region_id
        FROM (VALUES
            ('MPX-BKL-01', 'MPX Bengkulu', 'PT Internet Rakyat Makmur',
             'SUMATERA', 'BENGKULU'),
            ('MPX-SDA-02', 'MPX Sidoarjo', 'PT Internet Rakyat Makmur',
             'JAVA',      'SIDOARJO')
        ) AS v(code, name, legal_name, circle, region_code)
        LEFT JOIN region r ON r.region_code = v.region_code
        WHERE NOT EXISTS (SELECT 1 FROM mpx m WHERE m.code = v.code)
        """
    )


def downgrade() -> None:
    # Reverse dependency order. admin_refresh_token would cascade from admin_user, but
    # it is dropped explicitly so the intent survives a future reordering.
    op.execute("DROP TABLE IF EXISTS admin_refresh_token")
    op.execute("DROP TABLE IF EXISTS admin_user")
    op.execute("DROP TABLE IF EXISTS mpx")
    op.execute("DROP TABLE IF EXISTS device_partner")
