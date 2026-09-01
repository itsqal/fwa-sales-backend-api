"""Supply chain step 3 — commercial master data.

Adds ``call_plan``, ``brand`` and ``address``, and extends the existing
``device_model`` (Spec §7 step 3). ``fwa_inventory`` is deliberately untouched here;
that is step 4 and is the one migration that can affect the mobile app.

Three decisions worth reading before changing anything in this file.

**The device catalogue is the existing AE seed, and nothing else.** Confirmed
2026-09-01: `HKM 127+`, `RABIT CPE-XR`, `ADVAN V1 PRO`. The web mockups showed
`ZTE K12`, `Rabit` and `Rabit Unlimited`; those names are dropped rather than seeded,
because two rows for one physical device would silently split inventory and the AE
app's "Tipe Modem" lookup would start returning a model the dashboard never heard of.
`HKM 131 PRO` is named in the review documents but has never existed in this schema.
ZTE remains a ``device_partner`` — it has admin logins — with no catalogued model, so
no device PO can be raised against it until one is added.

**``device_model.brand`` is left exactly as it is.** It holds the manufacturer
(`HKM`, `RABIT`, `ADVAN`) and is already published to the mobile app through
``DeviceModelOut.brand``; renaming it would break a deployed client for a tidier name.
The new ``device_partner_id`` is the structured version of the same fact and is
backfilled from it. The *telco* brand — `IM3` / `3ID` — is a different concept in the
new ``brand`` table and is always referred to as ``brand_code``.

**``list_price_idr`` is nullable and seeded NULL.** The only prices available are demo
figures from the mockups, which the UI review itself flags as placeholder data. Picking
one would be inventing master data (CLAUDE.md §12). A model with no price cannot be
ordered; that is the same discipline as an uncategorised ``network_generation``
accruing no incentive rather than being defaulted into a tier. Setting real prices is
an UPDATE, not a migration.

Revision ID: 0004_commercial_master
Revises: 0003_supply_org_and_admin_auth
Create Date: 2026-09-01

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_commercial_master"
down_revision: str | None = "0003_supply_org_and_admin_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -----------------------------------------------------------------
    # SUPPLY CHAIN — MODULE F — Commercial master
    # -----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE call_plan (
            call_plan_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code         VARCHAR(30) NOT NULL,
            name         VARCHAR(60) NOT NULL,
            kind         VARCHAR(10) NOT NULL,
            quota_gb     INTEGER,
            is_active    BOOLEAN     NOT NULL DEFAULT TRUE,
            sort_order   SMALLINT    NOT NULL DEFAULT 0,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_call_plan_code  UNIQUE (code),
            CONSTRAINT ck_call_plan_kind  CHECK (kind IN ('DATA','BALANCE')),
            -- Saldo Mobo is a balance top-up, not a data quota. Encoding that here
            -- stops it becoming a special case in every consumer that reads quota_gb.
            CONSTRAINT ck_call_plan_quota CHECK (
                (kind = 'DATA'    AND quota_gb IS NOT NULL AND quota_gb > 0) OR
                (kind = 'BALANCE' AND quota_gb IS NULL)
            )
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE call_plan IS "
        "'Commercial plan attached to an MSISDN request. Backs the Call Plan dropdown "
        "on the DP Buat PO form.'"
    )
    op.execute(
        "COMMENT ON COLUMN call_plan.kind IS "
        "'DATA carries a quota_gb; BALANCE does not. The fourth dropdown option "
        "(Saldo Mobo) is a balance product, which is why quota_gb is nullable.'"
    )

    op.execute(
        """
        CREATE TABLE brand (
            code         VARCHAR(10) PRIMARY KEY,
            display_name VARCHAR(60) NOT NULL,
            outlet_name  VARCHAR(60) NOT NULL,
            is_active    BOOLEAN     NOT NULL DEFAULT TRUE,
            sort_order   SMALLINT    NOT NULL DEFAULT 0,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE brand IS "
        "'Telco retail brand. A natural-key primary key on purpose: there are exactly "
        "two rows, every referencing table names the column brand_code, and a UUID "
        "would make every PO join unreadable for no gain.'"
    )
    op.execute(
        "COMMENT ON COLUMN brand.outlet_name IS "
        "'The storefront name for the same brand — Gerai IM3, 3Store. The mockups used "
        "display_name and outlet_name interchangeably; holding both stops the drift.'"
    )

    op.execute(
        """
        CREATE TABLE address (
            address_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            mpx_id          UUID NOT NULL REFERENCES mpx(mpx_id) ON DELETE CASCADE,
            label           VARCHAR(60)  NOT NULL,
            recipient_name  VARCHAR(150) NOT NULL,
            recipient_phone VARCHAR(20)  NOT NULL,
            line1           TEXT         NOT NULL,
            kelurahan       VARCHAR(120),
            kecamatan       VARCHAR(120),
            city            VARCHAR(120) NOT NULL,
            province        VARCHAR(120) NOT NULL,
            postal_code     VARCHAR(10),
            latitude        DOUBLE PRECISION,
            longitude       DOUBLE PRECISION,
            gmaps_url       TEXT,
            is_default      BOOLEAN     NOT NULL DEFAULT FALSE,
            is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- Contact number, not an FWA MSISDN: stored as typed, 08xx or 62xx.
            -- Same rule as customer.phone_number.
            CONSTRAINT ck_address_phone CHECK (recipient_phone ~ '^(62|0)[0-9]{8,13}$'),
            CONSTRAINT ck_address_lat CHECK (latitude  IS NULL OR latitude  BETWEEN -90  AND 90),
            CONSTRAINT ck_address_lng CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180),
            -- Half a coordinate cannot be plotted, so it must not be storable.
            CONSTRAINT ck_address_geo_pair CHECK ((latitude IS NULL) = (longitude IS NULL))
        )
        """
    )
    # One default delivery address per MPX. A partial unique index rather than a trigger:
    # setting a new default is then "clear the old, set the new" in one transaction, and
    # two concurrent attempts cannot both win.
    op.execute("CREATE UNIQUE INDEX uq_address_one_default ON address (mpx_id) WHERE is_default")
    op.execute("CREATE INDEX ix_address_mpx ON address (mpx_id) WHERE is_active")
    op.execute(
        "COMMENT ON TABLE address IS "
        "'The MPX Alamat Penerima book. Every column here is rendered by the Alamat "
        "Lengkap modal. Coordinates and postal code are nullable because an address "
        "book entry is usable without them; recipient, line1, city and province are "
        "not, because a shipment cannot be delivered without them.'"
    )

    # -----------------------------------------------------------------
    # MODULE B — device_model gains the fields the dashboard needs.
    # -----------------------------------------------------------------
    op.execute(
        """
        ALTER TABLE device_model
            ADD COLUMN device_partner_id UUID REFERENCES device_partner(device_partner_id),
            ADD COLUMN list_price_idr    BIGINT,
            ADD COLUMN image_url         TEXT,
            ADD COLUMN is_active         BOOLEAN NOT NULL DEFAULT TRUE,
            ADD CONSTRAINT ck_device_model_price
                CHECK (list_price_idr IS NULL OR list_price_idr > 0)
        """
    )
    # The manufacturer has been recorded as free text since 0001. Promote it to a real
    # foreign key without touching the original column, which the mobile app reads.
    op.execute(
        """
        UPDATE device_model dm
           SET device_partner_id = dp.device_partner_id
          FROM device_partner dp
         WHERE upper(dm.brand) = dp.code
           AND dm.device_partner_id IS NULL
        """
    )
    op.execute(
        "COMMENT ON COLUMN device_model.brand IS "
        "'Manufacturer as free text (HKM, RABIT, ADVAN). Published to the mobile app "
        "as DeviceModelOut.brand, so it is not renamed. device_partner_id is the "
        "structured form of the same fact. NOT the telco brand — that is brand_code.'"
    )
    op.execute(
        "COMMENT ON COLUMN device_model.list_price_idr IS "
        "'Catalogue price in whole rupiah. Deliberately nullable: no confirmed price "
        "list exists yet, and a model without one cannot be ordered rather than being "
        "ordered at a guessed price. A device PO snapshots its own unit_price_idr, so "
        "changing this later never rewrites the value of a closed order.'"
    )

    # -----------------------------------------------------------------
    # Reference rows. Guarded, so a downgrade/re-upgrade cannot duplicate them.
    # -----------------------------------------------------------------
    op.execute(
        """
        INSERT INTO call_plan (code, name, kind, quota_gb, sort_order)
        SELECT v.code, v.name, v.kind, v.quota_gb, v.sort_order
        FROM (VALUES
            ('DATA_50GB',  '50 GB',       'DATA',    50,   1),
            ('DATA_75GB',  '75 GB',       'DATA',    75,   2),
            ('DATA_150GB', '150 GB',      'DATA',    150,  3),
            ('SALDO_MOBO', 'Saldo Mobo',  'BALANCE', NULL, 4)
        ) AS v(code, name, kind, quota_gb, sort_order)
        WHERE NOT EXISTS (SELECT 1 FROM call_plan c WHERE c.code = v.code)
        """
    )
    op.execute(
        """
        INSERT INTO brand (code, display_name, outlet_name, sort_order)
        SELECT v.code, v.display_name, v.outlet_name, v.sort_order
        FROM (VALUES
            ('IM3', 'IM3', 'Gerai IM3', 1),
            ('3ID', '3ID', '3Store',    2)
        ) AS v(code, display_name, outlet_name, sort_order)
        WHERE NOT EXISTS (SELECT 1 FROM brand b WHERE b.code = v.code)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE device_model
            DROP CONSTRAINT ck_device_model_price,
            DROP COLUMN is_active,
            DROP COLUMN image_url,
            DROP COLUMN list_price_idr,
            DROP COLUMN device_partner_id
        """
    )
    op.execute("DROP TABLE IF EXISTS address")
    op.execute("DROP TABLE IF EXISTS brand")
    op.execute("DROP TABLE IF EXISTS call_plan")
