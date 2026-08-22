-- =====================================================================
-- FWA Sales Digitalization — HiFi AIR
-- Account Executive (AE) Module — PostgreSQL 16 schema
--
-- Scope: AE only. GSE / DSE / DSF activities (including Open New Outlet)
--        are deliberately excluded.
-- Source: "IBRS Brief FWA in i-Sales" (slides 9, 15-17, 22-24, 26-28)
--         + HiFi AIR Sales Business Process / Digital Solution Description
--         + AE mobile UI mockups (8 screens)
--
-- Conventions
--   * snake_case identifiers, plural-free table names
--   * TIMESTAMPTZ everywhere; the server is authoritative on time
--   * Enumerations use VARCHAR + CHECK, not native ENUM types, so values
--     can be added in a plain transaction (the Edukasi/Purchase/Hot Leads
--     taxonomy is still under stakeholder discussion)
--   * Surrogate UUID primary keys for entities the API references by URL,
--     EXCEPT customer and activation, which use BIGINT identities so the
--     10-digit IDs shown in the UI ("1234567890 - Susanto") are real
--   * Every write table carries idempotency_key to make the mobile app's
--     offline outbox safe to retry
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()

-- ---------------------------------------------------------------------
-- MODULE A — Identity & Access
-- ---------------------------------------------------------------------

CREATE TABLE region (
    region_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    region_code     VARCHAR(50)  NOT NULL,
    region_name     VARCHAR(120) NOT NULL,
    province        VARCHAR(120),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_region_code UNIQUE (region_code)
);
COMMENT ON TABLE region IS
  'Sales region. AE codes embed the region name (AE-BENGKULU1, AE-SIDOARJO2).';

CREATE TABLE account_executive (
    ae_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ae_code          VARCHAR(50)  NOT NULL,   -- 'AE-BENGKULU1' — shown in every UI header
    full_name        VARCHAR(150) NOT NULL,
    password_hash    TEXT         NOT NULL,   -- argon2id / bcrypt
    phone_number     VARCHAR(20),
    email            VARCHAR(150),
    region_id        UUID REFERENCES region(region_id),
    mpx_code         VARCHAR(50),             -- distributor the AE draws stock from
    work_shift_start TIME NOT NULL DEFAULT '08:00',
    work_shift_end   TIME NOT NULL DEFAULT '18:00',
    status           VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
    must_change_pw   BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_ae_status CHECK (status IN ('ACTIVE','SUSPENDED','INACTIVE'))
);
-- Login is case-insensitive (brief slide 9: "Insensitive Case")
CREATE UNIQUE INDEX uq_ae_code_ci ON account_executive (upper(ae_code));
COMMENT ON COLUMN account_executive.ae_code IS
  'Natural key issued by IOH HQ. Format AE-<REGION><N>. Matched case-insensitively at login.';

CREATE TABLE auth_refresh_token (
    token_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ae_id         UUID NOT NULL REFERENCES account_executive(ae_id) ON DELETE CASCADE,
    token_hash    TEXT        NOT NULL,
    device_label  VARCHAR(120),
    issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ,
    CONSTRAINT uq_refresh_token_hash UNIQUE (token_hash)
);
CREATE INDEX ix_refresh_ae_active ON auth_refresh_token (ae_id) WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------------
-- MODULE B — Product & Inventory master
-- ---------------------------------------------------------------------

CREATE TABLE device_model (
    device_model_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_code      VARCHAR(60)  NOT NULL,   -- 'HKM 127+', 'RABIT CPE-XR', 'ADVAN V1 PRO'
    brand           VARCHAR(60),             -- 'HKM', 'RABIT', 'ADVAN'
    sku             VARCHAR(60),
    network_generation VARCHAR(10),          -- '4G' | '5G'; NULL = not categorised yet
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_device_model_code UNIQUE (model_code),
    CONSTRAINT ck_device_model_netgen CHECK
        (network_generation IS NULL OR network_generation IN ('4G','5G'))
);
COMMENT ON TABLE device_model IS
  'CPE / modem catalogue. Surfaces read-only as "Tipe Modem" on the activation form.';
COMMENT ON COLUMN device_model.network_generation IS
  'Radio generation of the CPE. Selects the incentive tier on a confirmed Gross Add: '
  'a 4G unit accrues Rp 35.000, a 5G unit Rp 135.000. Deliberately nullable — an '
  'uncategorised model accrues nothing and logs a warning, because underpaying is '
  'recoverable and overpaying is a payroll incident.';

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
);
CREATE INDEX ix_inv_allocated_ae ON fwa_inventory (allocated_ae_id, status);
COMMENT ON TABLE fwa_inventory IS
  'MSISDN-IMEI bundle produced by the Device Partner. This table is what makes the '
  '"scan MSISDN -> auto-fill IMEI + Tipe Modem" behaviour on the activation screen possible. '
  'Stock arrives pre-allocated to an AE; the PO/AWB request flow is out of scope for v1.';
COMMENT ON COLUMN fwa_inventory.msisdn IS
  'Normalised to 62 country-code form. The app must convert a scanned 08xx barcode before sending.';

-- ---------------------------------------------------------------------
-- MODULE C — Field activity  (backs the bare-minimum AE screens)
-- ---------------------------------------------------------------------

-- Screen: "Input New Customer" / "Daftar New Customer" / "Daftar Hot Leads"
-- Equivalent to the brief's AE "PJP" entity (slide 28): idAE, pjpDate,
-- namaCustomer, nomorHandphone, alamatRumah, checkIn, getLonglat, status.
CREATE TABLE customer (
    customer_id      BIGINT PRIMARY KEY
                       GENERATED ALWAYS AS IDENTITY (START WITH 1000000000),
    ae_id            UUID         NOT NULL REFERENCES account_executive(ae_id),
    visit_date       DATE         NOT NULL,          -- "Tanggal Pergi" / pjpDate
    full_name        VARCHAR(150) NOT NULL,          -- "Nama Customer"
    phone_number     VARCHAR(20)  NOT NULL,          -- "Nomor Handphone Customer"
    address          TEXT         NOT NULL,          -- "Alamat Customer" / alamatRumah
    latitude         DOUBLE PRECISION NOT NULL,
    longitude        DOUBLE PRECISION NOT NULL,
    geo_accuracy_m   NUMERIC(6,1),
    geo_verified     BOOLEAN      NOT NULL DEFAULT FALSE, -- "Longlat terverifikasi"
    is_mocked        BOOLEAN      NOT NULL DEFAULT FALSE, -- Android fake-GPS flag
    resolved_address TEXT,                               -- reverse-geocoded, audit only
    status           VARCHAR(20)  NOT NULL,             -- "Status Customer"
    visited_at       TIMESTAMPTZ  NOT NULL DEFAULT now(), -- brief's "checkIn" column
    idempotency_key  UUID,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_cust_status CHECK (status IN ('EDUKASI','PURCHASE','HOT_LEADS')),
    CONSTRAINT ck_cust_phone  CHECK (phone_number ~ '^(62|0)[0-9]{8,13}$'),
    CONSTRAINT ck_cust_lat    CHECK (latitude  BETWEEN -90  AND 90),
    CONSTRAINT ck_cust_lng    CHECK (longitude BETWEEN -180 AND 180),
    CONSTRAINT uq_cust_idem   UNIQUE (idempotency_key)
);
CREATE INDEX ix_cust_ae_date   ON customer (ae_id, visit_date DESC);
CREATE INDEX ix_cust_ae_status ON customer (ae_id, status, visit_date DESC);
CREATE INDEX ix_cust_geo       ON customer (latitude, longitude);
-- Guard against the same lead being registered twice by one AE
CREATE UNIQUE INDEX uq_cust_ae_phone ON customer (ae_id, phone_number);

COMMENT ON TABLE customer IS
  'A prospect registered by an AE during door-to-door activity. One row per lead. '
  'The 10-digit customer_id is the "ID Customer" shown in the activation dropdown.';
COMMENT ON COLUMN customer.status IS
  'Interaction outcome, i-Sales taxonomy: EDUKASI (educated, no intent), '
  'HOT_LEADS (high intent, follow-up needed), PURCHASE (agreed to buy). '
  'NOTE: the Digital Solution Description uses Potential / Sell In / Non-Potential '
  'for the same concept — the i-Sales taxonomy was selected as authoritative.';

-- Makes the funnel measurable: Hot Leads -> Purchase conversion over time.
CREATE TABLE customer_status_history (
    history_id   BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    customer_id  BIGINT      NOT NULL REFERENCES customer(customer_id) ON DELETE CASCADE,
    old_status   VARCHAR(20),
    new_status   VARCHAR(20) NOT NULL,
    changed_by   UUID        REFERENCES account_executive(ae_id),
    note         TEXT,
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_hist_new CHECK (new_status IN ('EDUKASI','PURCHASE','HOT_LEADS'))
);
CREATE INDEX ix_hist_customer ON customer_status_history (customer_id, changed_at DESC);

-- Screen: "Aktivasi Pelanggan" (input) / "Daftar Aktivasi"
-- This is the AE equivalent of the brief's "Sell In" entity (slide 24).
-- Where GSE sells in to a Gadget Store, the AE activates for a direct customer.
CREATE TABLE activation (
    activation_id     BIGINT PRIMARY KEY
                        GENERATED ALWAYS AS IDENTITY (START WITH 2900000000),
    ae_id             UUID        NOT NULL REFERENCES account_executive(ae_id),
    customer_id       BIGINT      NOT NULL REFERENCES customer(customer_id),
    msisdn            VARCHAR(15) NOT NULL REFERENCES fwa_inventory(msisdn),
    -- Snapshotted from inventory at submit time so history survives master-data edits
    imei              VARCHAR(16) NOT NULL,
    device_model_code VARCHAR(60),
    latitude          DOUBLE PRECISION NOT NULL,
    longitude         DOUBLE PRECISION NOT NULL,
    geo_accuracy_m    NUMERIC(6,1),
    geo_verified      BOOLEAN     NOT NULL DEFAULT FALSE,
    is_mocked         BOOLEAN     NOT NULL DEFAULT FALSE,
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),  -- "Sell In Date"
    status            VARCHAR(20) NOT NULL DEFAULT 'NOT_ACTIVATED',
    activation_date   TIMESTAMPTZ,                          -- "GA Date", fed from GCP
    ga_synced_at      TIMESTAMPTZ,
    idempotency_key   UUID,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_act_status CHECK (status IN ('NOT_ACTIVATED','ACTIVATED','FAILED','CANCELLED')),
    CONSTRAINT ck_act_ga     CHECK (status <> 'ACTIVATED' OR activation_date IS NOT NULL),
    CONSTRAINT uq_act_msisdn UNIQUE (msisdn),          -- an MSISDN can be sold once
    CONSTRAINT uq_act_idem   UNIQUE (idempotency_key)
);
CREATE INDEX ix_act_ae_date   ON activation (ae_id, submitted_at DESC);
CREATE INDEX ix_act_ae_status ON activation (ae_id, status, submitted_at DESC);
CREATE INDEX ix_act_customer  ON activation (customer_id);

COMMENT ON TABLE activation IS
  'A HiFi AIR unit handed to a direct customer by an AE. Created as NOT_ACTIVATED; '
  'flips to ACTIVATED when the Gross Add feed from GCP confirms the SIM is live. '
  'That gap is exactly the Activated / Not Activated badge on "Daftar Aktivasi".';
COMMENT ON COLUMN activation.activation_date IS
  'GA Date. Never written by the mobile app — populated by the GCP reconciliation job.';

-- ---------------------------------------------------------------------
-- MODULE D — Attendance (CICO)
-- In the brief's AE scope (slides 15-17) but not part of the
-- bare-minimum UI set. Included so the schema does not need a migration.
-- ---------------------------------------------------------------------

CREATE TABLE attendance (
    attendance_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ae_id                UUID NOT NULL REFERENCES account_executive(ae_id),
    attendance_date      DATE NOT NULL,
    check_in_at          TIMESTAMPTZ,
    check_in_photo_url   TEXT,                       -- selfie, mandatory on check-in
    check_in_lat         DOUBLE PRECISION,
    check_in_lng         DOUBLE PRECISION,
    check_in_address     TEXT,
    check_in_is_mocked   BOOLEAN NOT NULL DEFAULT FALSE,
    within_geofence      BOOLEAN,
    check_out_at         TIMESTAMPTZ,
    check_out_lat        DOUBLE PRECISION,
    check_out_lng        DOUBLE PRECISION,
    note                 TEXT,                       -- "Catatan (Opsional)"
    document_url         TEXT,                       -- "Dokumen (Opsional)"
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
);
COMMENT ON COLUMN attendance.approval_status IS
  'Out-of-geofence check-in is not rejected outright — it is routed for CSE approval, '
  'per the UI warning "Lokasi kamu berada di luar jangkauan".';

-- ---------------------------------------------------------------------
-- MODULE E — Targets & Incentive
-- Backs the home-screen chart (actual vs target) and the
-- "Total Aktivasi / Insentif / New Customer / Hot Leads" tiles.
-- ---------------------------------------------------------------------

CREATE TABLE ae_daily_target (
    target_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ae_id          UUID NOT NULL REFERENCES account_executive(ae_id),
    target_date    DATE NOT NULL,
    target_activations INTEGER NOT NULL DEFAULT 0,
    target_new_customers INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_target_ae_date UNIQUE (ae_id, target_date),
    CONSTRAINT ck_target_nonneg  CHECK (target_activations >= 0)
);
COMMENT ON TABLE ae_daily_target IS
  'Supplies the grey benchmark bars behind the pink actual bars on the home chart.';

CREATE TABLE incentive_rule (
    rule_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_name      VARCHAR(120) NOT NULL,
    event_type     VARCHAR(30)  NOT NULL,
    amount_idr     NUMERIC(14,2) NOT NULL,
    conditions     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    effective_from DATE         NOT NULL,
    effective_to   DATE,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_rule_event CHECK (event_type IN ('ACTIVATION','NEW_CUSTOMER','HOT_LEAD')),
    CONSTRAINT ck_rule_dates CHECK (effective_to IS NULL OR effective_to >= effective_from)
);
COMMENT ON COLUMN incentive_rule.conditions IS
  'Predicate the accrual engine must satisfy before writing a ledger entry. Exactly one '
  'key is implemented: "networkGeneration" ("4G" | "5G"), matched against the device '
  'model behind the activated MSISDN. An empty object always matches. A rule carrying '
  'any other key is skipped and logged — an unevaluable rule must never pay out.';

-- The AE activation tiers agreed with the business on 2026-08-22. Held as data, not
-- code: a future revision is a new pair of rows with a later effective_from, and the
-- ledger keeps pointing at the rule that actually paid.
INSERT INTO incentive_rule (rule_name, event_type, amount_idr, conditions, effective_from)
VALUES
  ('AE GA activation - 4G modem', 'ACTIVATION',  35000.00, '{"networkGeneration": "4G"}', DATE '2026-08-22'),
  ('AE GA activation - 5G modem', 'ACTIVATION', 135000.00, '{"networkGeneration": "5G"}', DATE '2026-08-22');

CREATE TABLE incentive_ledger (
    ledger_id     BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    ae_id         UUID NOT NULL REFERENCES account_executive(ae_id),
    rule_id       UUID REFERENCES incentive_rule(rule_id),
    activation_id BIGINT REFERENCES activation(activation_id),
    customer_id   BIGINT REFERENCES customer(customer_id),
    amount_idr    NUMERIC(14,2) NOT NULL,
    earned_date   DATE        NOT NULL,
    period_ym     CHAR(7)     NOT NULL,          -- '2026-08'
    status        VARCHAR(20) NOT NULL DEFAULT 'ACCRUED',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_ledger_status CHECK (status IN ('ACCRUED','APPROVED','PAID','VOID')),
    -- one payout per rule per source event
    CONSTRAINT uq_ledger_event UNIQUE (rule_id, activation_id, customer_id)
);
CREATE INDEX ix_ledger_ae_period ON incentive_ledger (ae_id, period_ym);
COMMENT ON TABLE incentive_ledger IS
  'Append-only accrual of AE earnings. The "Insentif" tile is SUM(amount_idr) '
  'over the selected period. Displayed in hundreds of thousands of rupiah.';

-- ---------------------------------------------------------------------
-- Reporting helper — backs GET /reports/daily-activity
--
-- Implemented as a range-parameterised function rather than a view. A view
-- would have to hardcode its own date window, which silently returns zero
-- rows for any period outside it and cross-joins every AE on every query.
-- The date spine is generated inside the requested range so the chart keeps
-- an unbroken axis across days with no activity.
-- ---------------------------------------------------------------------

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
$$;

COMMENT ON FUNCTION fn_ae_daily_activity IS
  'One row per day in [p_from, p_to] for the home-screen chart: '
  'activations (pink bars) against target_activations (grey bars).';

-- ---------------------------------------------------------------------
-- Optional: enable when the AI Sales Hotspot work starts
-- ---------------------------------------------------------------------
-- CREATE EXTENSION IF NOT EXISTS postgis;
-- ALTER TABLE customer   ADD COLUMN geom geography(Point,4326)
--   GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude),4326)::geography) STORED;
-- CREATE INDEX ix_cust_geom ON customer USING GIST (geom);
