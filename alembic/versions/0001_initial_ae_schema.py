"""Initial AE schema — identity, inventory, field activity, attendance, incentive.

This migration is hand-written, and deliberately so. It is a transcription of
``docs/schema.sql``, which is the model of record and has already been applied to a live
PostgreSQL and tested against real sample data. Autogenerate cannot see most of what
matters here — the functional unique index on ``upper(ae_code)``, every CHECK
constraint, the partial index on active refresh tokens, the identity sequences that
start at 1000000000 and 2900000000, and ``fn_ae_daily_activity`` — so generating it
would have produced a schema that looks right and behaves differently.

Revision ID: 0001_initial_ae_schema
Revises:
Create Date: 2026-08-19

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial_ae_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    # -----------------------------------------------------------------
    # MODULE A — Identity & Access
    # -----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE region (
            region_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            region_code     VARCHAR(50)  NOT NULL,
            region_name     VARCHAR(120) NOT NULL,
            province        VARCHAR(120),
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_region_code UNIQUE (region_code)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE region IS "
        "'Sales region. AE codes embed the region name (AE-BENGKULU1, AE-SIDOARJO2).'"
    )

    op.execute(
        """
        CREATE TABLE account_executive (
            ae_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ae_code          VARCHAR(50)  NOT NULL,
            full_name        VARCHAR(150) NOT NULL,
            password_hash    TEXT         NOT NULL,
            phone_number     VARCHAR(20),
            email            VARCHAR(150),
            region_id        UUID REFERENCES region(region_id),
            mpx_code         VARCHAR(50),
            work_shift_start TIME NOT NULL DEFAULT '08:00',
            work_shift_end   TIME NOT NULL DEFAULT '18:00',
            status           VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
            must_change_pw   BOOLEAN      NOT NULL DEFAULT TRUE,
            created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT ck_ae_status CHECK (status IN ('ACTIVE','SUSPENDED','INACTIVE'))
        )
        """
    )
    # Login is case-insensitive. A plain UNIQUE(ae_code) would let AE-BENGKULU1 and
    # ae-bengkulu1 both exist and then both match at login.
    op.execute("CREATE UNIQUE INDEX uq_ae_code_ci ON account_executive (upper(ae_code))")
    op.execute(
        "COMMENT ON COLUMN account_executive.ae_code IS "
        "'Natural key issued by IOH HQ. Format AE-<REGION><N>. "
        "Matched case-insensitively at login.'"
    )

    op.execute(
        """
        CREATE TABLE auth_refresh_token (
            token_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ae_id         UUID NOT NULL REFERENCES account_executive(ae_id) ON DELETE CASCADE,
            token_hash    TEXT        NOT NULL,
            device_label  VARCHAR(120),
            issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at    TIMESTAMPTZ NOT NULL,
            revoked_at    TIMESTAMPTZ,
            CONSTRAINT uq_refresh_token_hash UNIQUE (token_hash)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_refresh_ae_active ON auth_refresh_token (ae_id) WHERE revoked_at IS NULL"
    )

    # -----------------------------------------------------------------
    # MODULE B — Product & Inventory master
    # -----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE device_model (
            device_model_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            model_code      VARCHAR(60)  NOT NULL,
            brand           VARCHAR(60),
            sku             VARCHAR(60),
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_device_model_code UNIQUE (model_code)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE device_model IS "
        "'CPE / modem catalogue. Surfaces read-only as \"Tipe Modem\" on the activation form.'"
    )

    op.execute(
        """
        CREATE TABLE fwa_inventory (
            msisdn            VARCHAR(15) PRIMARY KEY,
            iccid             VARCHAR(22),
            imei              VARCHAR(16) NOT NULL,
            device_model_id   UUID REFERENCES device_model(device_model_id),
            allocated_ae_id   UUID REFERENCES account_executive(ae_id),
            allocated_at      TIMESTAMPTZ,
            status            VARCHAR(20) NOT NULL DEFAULT 'AVAILABLE',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_inv_msisdn CHECK (msisdn ~ '^62[0-9]{8,13}$'),
            CONSTRAINT ck_inv_imei   CHECK (imei   ~ '^[0-9]{14,16}$'),
            CONSTRAINT ck_inv_iccid  CHECK (iccid IS NULL OR iccid ~ '^[0-9]{18,20}$'),
            CONSTRAINT ck_inv_status CHECK (status IN
                ('AVAILABLE','ALLOCATED','CONSUMED','ACTIVATED','RETURNED','BLOCKED')),
            CONSTRAINT uq_inv_imei  UNIQUE (imei),
            CONSTRAINT uq_inv_iccid UNIQUE (iccid)
        )
        """
    )
    op.execute("CREATE INDEX ix_inv_allocated_ae ON fwa_inventory (allocated_ae_id, status)")
    op.execute(
        "COMMENT ON COLUMN fwa_inventory.msisdn IS "
        "'Normalised to 62 country-code form. The app must convert a scanned 08xx "
        "barcode before sending.'"
    )

    # -----------------------------------------------------------------
    # MODULE C — Field activity
    # -----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE customer (
            customer_id      BIGINT PRIMARY KEY
                               GENERATED ALWAYS AS IDENTITY (START WITH 1000000000),
            ae_id            UUID         NOT NULL REFERENCES account_executive(ae_id),
            visit_date       DATE         NOT NULL,
            full_name        VARCHAR(150) NOT NULL,
            phone_number     VARCHAR(20)  NOT NULL,
            address          TEXT         NOT NULL,
            latitude         DOUBLE PRECISION NOT NULL,
            longitude        DOUBLE PRECISION NOT NULL,
            geo_accuracy_m   NUMERIC(6,1),
            geo_verified     BOOLEAN      NOT NULL DEFAULT FALSE,
            is_mocked        BOOLEAN      NOT NULL DEFAULT FALSE,
            resolved_address TEXT,
            status           VARCHAR(20)  NOT NULL,
            visited_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
            idempotency_key  UUID,
            created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT ck_cust_status CHECK (status IN ('EDUKASI','PURCHASE','HOT_LEADS')),
            CONSTRAINT ck_cust_phone  CHECK (phone_number ~ '^(62|0)[0-9]{8,13}$'),
            CONSTRAINT ck_cust_lat    CHECK (latitude  BETWEEN -90  AND 90),
            CONSTRAINT ck_cust_lng    CHECK (longitude BETWEEN -180 AND 180),
            CONSTRAINT uq_cust_idem   UNIQUE (idempotency_key)
        )
        """
    )
    op.execute("CREATE INDEX ix_cust_ae_date   ON customer (ae_id, visit_date DESC)")
    op.execute("CREATE INDEX ix_cust_ae_status ON customer (ae_id, status, visit_date DESC)")
    op.execute("CREATE INDEX ix_cust_geo       ON customer (latitude, longitude)")
    op.execute("CREATE UNIQUE INDEX uq_cust_ae_phone ON customer (ae_id, phone_number)")
    op.execute(
        "COMMENT ON COLUMN customer.status IS "
        "'Interaction outcome, i-Sales taxonomy: EDUKASI (educated, no intent), "
        "HOT_LEADS (high intent, follow-up needed), PURCHASE (agreed to buy).'"
    )

    op.execute(
        """
        CREATE TABLE customer_status_history (
            history_id   BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
            customer_id  BIGINT      NOT NULL REFERENCES customer(customer_id) ON DELETE CASCADE,
            old_status   VARCHAR(20),
            new_status   VARCHAR(20) NOT NULL,
            changed_by   UUID        REFERENCES account_executive(ae_id),
            note         TEXT,
            changed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_hist_new CHECK (new_status IN ('EDUKASI','PURCHASE','HOT_LEADS'))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_hist_customer ON customer_status_history (customer_id, changed_at DESC)"
    )

    op.execute(
        """
        CREATE TABLE activation (
            activation_id     BIGINT PRIMARY KEY
                                GENERATED ALWAYS AS IDENTITY (START WITH 2900000000),
            ae_id             UUID        NOT NULL REFERENCES account_executive(ae_id),
            customer_id       BIGINT      NOT NULL REFERENCES customer(customer_id),
            msisdn            VARCHAR(15) NOT NULL REFERENCES fwa_inventory(msisdn),
            imei              VARCHAR(16) NOT NULL,
            device_model_code VARCHAR(60),
            latitude          DOUBLE PRECISION NOT NULL,
            longitude         DOUBLE PRECISION NOT NULL,
            geo_accuracy_m    NUMERIC(6,1),
            geo_verified      BOOLEAN     NOT NULL DEFAULT FALSE,
            is_mocked         BOOLEAN     NOT NULL DEFAULT FALSE,
            submitted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            status            VARCHAR(20) NOT NULL DEFAULT 'NOT_ACTIVATED',
            activation_date   TIMESTAMPTZ,
            ga_synced_at      TIMESTAMPTZ,
            idempotency_key   UUID,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_act_status CHECK
                (status IN ('NOT_ACTIVATED','ACTIVATED','FAILED','CANCELLED')),
            CONSTRAINT ck_act_ga     CHECK (status <> 'ACTIVATED' OR activation_date IS NOT NULL),
            CONSTRAINT uq_act_msisdn UNIQUE (msisdn),
            CONSTRAINT uq_act_idem   UNIQUE (idempotency_key)
        )
        """
    )
    op.execute("CREATE INDEX ix_act_ae_date   ON activation (ae_id, submitted_at DESC)")
    op.execute("CREATE INDEX ix_act_ae_status ON activation (ae_id, status, submitted_at DESC)")
    op.execute("CREATE INDEX ix_act_customer  ON activation (customer_id)")
    op.execute(
        "COMMENT ON COLUMN activation.activation_date IS "
        "'GA Date. Never written by the mobile app — populated by the GCP "
        "reconciliation job.'"
    )

    # -----------------------------------------------------------------
    # MODULE D — Attendance (CICO)
    # -----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE attendance (
            attendance_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ae_id                UUID NOT NULL REFERENCES account_executive(ae_id),
            attendance_date      DATE NOT NULL,
            check_in_at          TIMESTAMPTZ,
            check_in_photo_url   TEXT,
            check_in_lat         DOUBLE PRECISION,
            check_in_lng         DOUBLE PRECISION,
            check_in_address     TEXT,
            check_in_is_mocked   BOOLEAN NOT NULL DEFAULT FALSE,
            within_geofence      BOOLEAN,
            check_out_at         TIMESTAMPTZ,
            check_out_lat        DOUBLE PRECISION,
            check_out_lng        DOUBLE PRECISION,
            note                 TEXT,
            document_url         TEXT,
            approval_status      VARCHAR(20) NOT NULL DEFAULT 'AUTO_APPROVED',
            approval_note        TEXT,
            idempotency_key      UUID,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_att_approval CHECK (approval_status IN
                ('AUTO_APPROVED','PENDING_APPROVAL','APPROVED','REJECTED')),
            CONSTRAINT ck_att_order CHECK (check_out_at IS NULL OR check_in_at IS NULL
                                           OR check_out_at >= check_in_at),
            CONSTRAINT uq_att_ae_date UNIQUE (ae_id, attendance_date),
            CONSTRAINT uq_att_idem    UNIQUE (idempotency_key)
        )
        """
    )
    op.execute(
        "COMMENT ON COLUMN attendance.approval_status IS "
        "'Out-of-geofence check-in is not rejected outright — it is routed for CSE "
        'approval, per the UI warning "Lokasi kamu berada di luar jangkauan".\''
    )

    # -----------------------------------------------------------------
    # MODULE E — Targets & Incentive
    # -----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE ae_daily_target (
            target_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ae_id          UUID NOT NULL REFERENCES account_executive(ae_id),
            target_date    DATE NOT NULL,
            target_activations INTEGER NOT NULL DEFAULT 0,
            target_new_customers INTEGER NOT NULL DEFAULT 0,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_target_ae_date UNIQUE (ae_id, target_date),
            CONSTRAINT ck_target_nonneg  CHECK (target_activations >= 0)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE ae_daily_target IS "
        "'Supplies the grey benchmark bars behind the pink actual bars on the home chart.'"
    )

    op.execute(
        """
        CREATE TABLE incentive_rule (
            rule_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            rule_name      VARCHAR(120) NOT NULL,
            event_type     VARCHAR(30)  NOT NULL,
            amount_idr     NUMERIC(14,2) NOT NULL,
            conditions     JSONB        NOT NULL DEFAULT '{}'::jsonb,
            effective_from DATE         NOT NULL,
            effective_to   DATE,
            created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT ck_rule_event CHECK
                (event_type IN ('ACTIVATION','NEW_CUSTOMER','HOT_LEAD')),
            CONSTRAINT ck_rule_dates CHECK (effective_to IS NULL OR effective_to >= effective_from)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE incentive_ledger (
            ledger_id     BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
            ae_id         UUID NOT NULL REFERENCES account_executive(ae_id),
            rule_id       UUID REFERENCES incentive_rule(rule_id),
            activation_id BIGINT REFERENCES activation(activation_id),
            customer_id   BIGINT REFERENCES customer(customer_id),
            amount_idr    NUMERIC(14,2) NOT NULL,
            earned_date   DATE        NOT NULL,
            period_ym     CHAR(7)     NOT NULL,
            status        VARCHAR(20) NOT NULL DEFAULT 'ACCRUED',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_ledger_status CHECK (status IN ('ACCRUED','APPROVED','PAID','VOID')),
            CONSTRAINT uq_ledger_event UNIQUE (rule_id, activation_id, customer_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_ledger_ae_period ON incentive_ledger (ae_id, period_ym)")
    op.execute(
        "COMMENT ON TABLE incentive_ledger IS "
        '\'Append-only accrual of AE earnings. The "Insentif" tile is SUM(amount_idr) '
        "over the selected period.'"
    )

    # -----------------------------------------------------------------
    # Reporting helper — backs GET /reports/daily-activity
    #
    # A function rather than a view: a view would have to hardcode its own date
    # window, silently returning zero rows outside it, and would cross-join every AE
    # on every query. The date spine is generated inside the requested range so the
    # chart keeps an unbroken axis across days with no activity.
    # -----------------------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION fn_ae_daily_activity(
            p_ae_id UUID,
            p_from  DATE,
            p_to    DATE
        )
        RETURNS TABLE (
            activity_date      DATE,
            activations        BIGINT,
            target_activations INTEGER,
            new_customers      BIGINT,
            hot_leads          BIGINT
        )
        LANGUAGE sql STABLE AS $$
            SELECT
                d.day::date,
                COUNT(DISTINCT act.activation_id),
                COALESCE(MAX(t.target_activations), 0),
                COUNT(DISTINCT c.customer_id),
                COUNT(DISTINCT c.customer_id) FILTER (WHERE c.status = 'HOT_LEADS')
            FROM generate_series(p_from, p_to, INTERVAL '1 day') AS d(day)
            LEFT JOIN activation act
                   ON act.ae_id = p_ae_id AND act.submitted_at::date = d.day::date
            LEFT JOIN customer c
                   ON c.ae_id = p_ae_id AND c.visit_date = d.day::date
            LEFT JOIN ae_daily_target t
                   ON t.ae_id = p_ae_id AND t.target_date = d.day::date
            GROUP BY d.day
            ORDER BY d.day;
        $$
        """
    )
    op.execute(
        "COMMENT ON FUNCTION fn_ae_daily_activity IS "
        "'One row per day in [p_from, p_to] for the home-screen chart: "
        "activations (pink bars) against target_activations (grey bars).'"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS fn_ae_daily_activity(UUID, DATE, DATE)")
    for table in (
        "incentive_ledger",
        "incentive_rule",
        "ae_daily_target",
        "attendance",
        "activation",
        "customer_status_history",
        "customer",
        "fwa_inventory",
        "device_model",
        "auth_refresh_token",
        "account_executive",
        "region",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
