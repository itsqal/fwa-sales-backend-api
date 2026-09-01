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
    -- Added 2026-09-01 (migration 0008). Which telco brands this AE may sell; HYBRID
    -- means either. A capability of the person, NOT a brand — deliberately not a row in
    -- the brand table, because a physical SIM is one brand or the other. Read only by
    -- the dashboard; the mobile contract is unchanged by its presence. NULL = not
    -- recorded, and nothing branches on it in v1.
    brand_scope      VARCHAR(10),
    work_shift_start TIME NOT NULL DEFAULT '08:00',
    work_shift_end   TIME NOT NULL DEFAULT '18:00',
    status           VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
    must_change_pw   BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT ck_ae_status CHECK (status IN ('ACTIVE','SUSPENDED','INACTIVE')),
    CONSTRAINT ck_ae_brand_scope CHECK
        (brand_scope IS NULL OR brand_scope IN ('IM3','3ID','HYBRID'))
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
    -- Added 2026-09-01 by the supply chain extension (migration 0004).
    device_partner_id UUID REFERENCES device_partner(device_partner_id),
    list_price_idr  BIGINT,                  -- whole rupiah; NULL = not priced, not orderable
    image_url       TEXT,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_device_model_code UNIQUE (model_code),
    CONSTRAINT ck_device_model_netgen CHECK
        (network_generation IS NULL OR network_generation IN ('4G','5G')),
    CONSTRAINT ck_device_model_price CHECK (list_price_idr IS NULL OR list_price_idr > 0)
);
-- NOTE: device_partner is declared further down, in the supply chain section. The FK
-- above is added by migration 0004, after that table exists.
COMMENT ON COLUMN device_model.brand IS
  'Manufacturer as free text (HKM, RABIT, ADVAN). Published to the mobile app as '
  'DeviceModelOut.brand, so it is not renamed. device_partner_id is the structured '
  'form of the same fact. NOT the telco brand — that is brand_code -> brand.code.';
COMMENT ON COLUMN device_model.list_price_idr IS
  'Catalogue price in whole rupiah. Deliberately nullable: no confirmed price list '
  'exists yet, and a model without one cannot be ordered rather than being ordered at '
  'a guessed price. A device PO snapshots its own unit_price_idr, so changing this '
  'later never rewrites the value of a closed order.';
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
    -- Nullable since migration 0005: a number IOH has supplied has no IMEI until the
    -- Device Partner pairs one to it. imei_required_once_paired confines that to the
    -- single pre-pairing state, so a NULL can never reach a status the AE app renders.
    imei              VARCHAR(16),
    device_model_id   UUID REFERENCES device_model(device_model_id),
    allocated_ae_id   UUID REFERENCES account_executive(ae_id),
    allocated_at      TIMESTAMPTZ,
    status            VARCHAR(20) NOT NULL DEFAULT 'AVAILABLE',
    -- Supply chain lifecycle, added by migration 0005. msisdn_po_id and device_po_id
    -- carry no FK until migrations 0006 and 0008 create their target tables.
    msisdn_po_id       UUID,
    device_po_id       UUID,
    mpx_id             UUID REFERENCES mpx(mpx_id),
    brand_code         VARCHAR(10) REFERENCES brand(code),
    call_plan_id       UUID REFERENCES call_plan(call_plan_id),
    paired_at          TIMESTAMPTZ,
    paired_by_admin_id UUID REFERENCES admin_user(admin_user_id),
    received_at        TIMESTAMPTZ,   -- FIFO key for automatic allocation
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_inv_msisdn CHECK (msisdn ~ '^62[0-9]{8,13}$'),
    -- Tolerates NULL: a regex CHECK is UNKNOWN on NULL, and a CHECK passes on UNKNOWN.
    CONSTRAINT ck_inv_imei   CHECK (imei   ~ '^[0-9]{14,16}$'),
    CONSTRAINT ck_inv_iccid  CHECK (iccid IS NULL OR iccid ~ '^[0-9]{18,20}$'),
    CONSTRAINT ck_inv_status CHECK (status IN
        ('AVAILABLE','ALLOCATED','CONSUMED','ACTIVATED','RETURNED','BLOCKED',
         'MSISDN_ISSUED','PAIRED','ASSIGNED','SHIPPED','RECEIVED')),
    CONSTRAINT imei_required_once_paired CHECK (status = 'MSISDN_ISSUED' OR imei IS NOT NULL),
    -- UNIQUE permits many NULLs, so unpaired numbers do not collide with one another.
    CONSTRAINT uq_inv_imei  UNIQUE (imei),
    CONSTRAINT uq_inv_iccid UNIQUE (iccid)
);
CREATE INDEX ix_inv_allocated_ae ON fwa_inventory (allocated_ae_id, status);
CREATE INDEX ix_inv_msisdn_po ON fwa_inventory (msisdn_po_id) WHERE msisdn_po_id IS NOT NULL;
CREATE INDEX ix_inv_device_po ON fwa_inventory (device_po_id) WHERE device_po_id IS NOT NULL;
CREATE INDEX ix_inv_mpx_status ON fwa_inventory (mpx_id, status);
-- Backs the AUTO allocation rule: FIFO on receipt date, oldest first.
CREATE INDEX ix_inv_allocatable ON fwa_inventory (mpx_id, received_at, msisdn)
  WHERE status = 'RECEIVED';
COMMENT ON TABLE fwa_inventory IS
  'The record of one HiFi AIR unit for its whole life: an MSISDN IOH issued, an IMEI the '
  'Device Partner paired to it, the orders it moved on, and the AE it was finally '
  'allocated to. Single source of truth for where a unit is — PO tables carry quantities '
  'and money, never a second copy of unit state.';
COMMENT ON COLUMN fwa_inventory.status IS
  'Lifecycle position. MSISDN_ISSUED -> PAIRED -> ASSIGNED -> SHIPPED -> RECEIVED belong '
  'to the supply chain and are invisible to the mobile app. ALLOCATED is the seam: it is '
  'written only by POST /admin/allocations and is the first state a salesman can see. '
  'CONSUMED is written by the activation path and MUST stay AE-visible, or a rescanned '
  'box reports "not a registered unit" instead of "already activated". AVAILABLE is '
  'deprecated, retained because live rows still carry it. '
  'See AE_VISIBLE_STATUSES in app/services/inventory.py.';
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

-- =====================================================================
-- SUPPLY CHAIN EXTENSION
--
-- Added 2026-09-01. Source: "Supply Chain Backend Extension Spec.md".
-- Serves the admin web dashboard (Device Partner / IOH / MPX), which is
-- UPSTREAM of the AE mobile app: nothing here is visible to a salesman
-- until an MPX admin allocates a unit, which sets
-- fwa_inventory.allocated_ae_id.
--
-- Module letters below are the SPEC's, not this file's. The AE model
-- already uses MODULE E for Targets & Incentive, so supply-chain modules
-- are qualified to keep both sets of cross-references readable.
-- =====================================================================

-- ---------------------------------------------------------------------
-- SUPPLY CHAIN — MODULE E — Organisations
-- ---------------------------------------------------------------------

CREATE TABLE device_partner (
    device_partner_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code              VARCHAR(20)  NOT NULL,   -- 'ADVAN' — prefix of its MSISDN PO numbers
    name              VARCHAR(150) NOT NULL,
    status            VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_device_partner_code   UNIQUE (code),
    CONSTRAINT ck_device_partner_code   CHECK (code ~ '^[A-Z0-9]{2,20}$'),
    CONSTRAINT ck_device_partner_status CHECK (status IN ('ACTIVE','INACTIVE'))
);
COMMENT ON TABLE device_partner IS
  'Hardware supplier that requests MSISDNs from IOH, pairs them to IMEIs, and fulfils MPX device orders.';
COMMENT ON COLUMN device_partner.code IS
  'Prefix of every MSISDN PO number this partner raises: ADVAN-20250523-207.';

CREATE TABLE mpx (
    mpx_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code         VARCHAR(20)  NOT NULL,   -- 'MPX-BKL-01'
    name         VARCHAR(150) NOT NULL,
    legal_name   VARCHAR(200),            -- 'PT Internet Rakyat Makmur' — MPX topbar
    circle       VARCHAR(20),             -- 'JAVA' | 'KALISUMAPA' | 'SUMATERA'
    region_id    UUID REFERENCES region(region_id),
    external_ref VARCHAR(200),            -- legacy identifier from an upstream system
    status       VARCHAR(20)  NOT NULL DEFAULT 'ACTIVE',
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_mpx_code   UNIQUE (code),
    CONSTRAINT ck_mpx_code   CHECK (code ~ '^MPX-[A-Z]{2,4}-[0-9]{2}$'),
    CONSTRAINT ck_mpx_circle CHECK (circle IS NULL OR circle IN ('JAVA','KALISUMAPA','SUMATERA')),
    CONSTRAINT ck_mpx_status CHECK (status IN ('ACTIVE','INACTIVE'))
);
COMMENT ON TABLE mpx IS
  'Distributor / stock point. Orders devices from a Device Partner, confirms receipt, '
  'and allocates units to Account Executives.';
COMMENT ON COLUMN mpx.code IS
  'Canonical identifier, format MPX-<AREA>-<NN>. Joined to account_executive.mpx_code, '
  'which already carries this format — so an MPX admin can list its own AEs without a '
  'schema change to the live AE table.';
COMMENT ON COLUMN mpx.external_ref IS
  'Identifier as it appears in an upstream source system. Display and search use code '
  'and name; this column exists so a legacy string never has to be parsed.';

-- ---------------------------------------------------------------------
-- SUPPLY CHAIN — Admin principals
--
-- Deliberately NOT a unified app_user with account_executive. The mobile
-- app is already in the field; unifying would touch its live auth path and
-- require backfilling every AE. Admin tokens carry aud="admin" and are
-- rejected by AE routes; AE tokens are rejected by /admin routes.
-- ---------------------------------------------------------------------

CREATE TABLE admin_user (
    admin_user_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username          VARCHAR(50)  NOT NULL,
    email             VARCHAR(150),
    password_hash     TEXT         NOT NULL,   -- argon2id
    full_name         VARCHAR(150) NOT NULL,   -- rendered in every attestation checkbox
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
);
-- Login is case-insensitive, same treatment as uq_ae_code_ci.
CREATE UNIQUE INDEX uq_admin_username_ci ON admin_user (upper(username));
CREATE UNIQUE INDEX uq_admin_email_ci    ON admin_user (upper(email)) WHERE email IS NOT NULL;
COMMENT ON CONSTRAINT ck_admin_org_binding ON admin_user IS
  'The binding is exactly as wide as the role allows. Enforced in the database rather '
  'than in Python because this is the constraint that separates three competing '
  'companies reading one dataset: an MPX admin who acquired a device_partner_id could '
  'read every competitor''s order book.';

CREATE TABLE admin_refresh_token (
    token_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_user_id UUID NOT NULL REFERENCES admin_user(admin_user_id) ON DELETE CASCADE,
    token_hash    TEXT        NOT NULL,
    device_label  VARCHAR(120),
    issued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ,
    CONSTRAINT uq_admin_refresh_token_hash UNIQUE (token_hash)
);
CREATE INDEX ix_admin_refresh_active ON admin_refresh_token (admin_user_id) WHERE revoked_at IS NULL;
COMMENT ON TABLE admin_refresh_token IS
  'Mirrors auth_refresh_token exactly — one row per browser session, digest only, revocable per device.';


-- ---------------------------------------------------------------------
-- SUPPLY CHAIN — MODULE F — Commercial master
-- ---------------------------------------------------------------------

CREATE TABLE call_plan (
    call_plan_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code         VARCHAR(30) NOT NULL,   -- 'DATA_50GB', 'SALDO_MOBO'
    name         VARCHAR(60) NOT NULL,   -- '50 GB', 'Saldo Mobo'
    kind         VARCHAR(10) NOT NULL,   -- 'DATA' | 'BALANCE'
    quota_gb     INTEGER,                -- NULL for BALANCE products
    is_active    BOOLEAN     NOT NULL DEFAULT TRUE,
    sort_order   SMALLINT    NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_call_plan_code  UNIQUE (code),
    CONSTRAINT ck_call_plan_kind  CHECK (kind IN ('DATA','BALANCE')),
    CONSTRAINT ck_call_plan_quota CHECK (
        (kind = 'DATA'    AND quota_gb IS NOT NULL AND quota_gb > 0) OR
        (kind = 'BALANCE' AND quota_gb IS NULL)
    )
);
COMMENT ON COLUMN call_plan.kind IS
  'DATA carries a quota_gb; BALANCE does not. The fourth dropdown option (Saldo Mobo) '
  'is a balance product, which is why quota_gb is nullable — and why the pairing is '
  'enforced here rather than re-checked by every consumer.';

INSERT INTO call_plan (code, name, kind, quota_gb, sort_order) VALUES
  ('DATA_50GB',  '50 GB',      'DATA',     50,  1),
  ('DATA_75GB',  '75 GB',      'DATA',     75,  2),
  ('DATA_150GB', '150 GB',     'DATA',    150,  3),
  ('SALDO_MOBO', 'Saldo Mobo', 'BALANCE', NULL, 4);

CREATE TABLE brand (
    code         VARCHAR(10) PRIMARY KEY,   -- 'IM3', '3ID'
    display_name VARCHAR(60) NOT NULL,      -- 'IM3', '3ID'
    outlet_name  VARCHAR(60) NOT NULL,      -- 'Gerai IM3', '3Store'
    is_active    BOOLEAN     NOT NULL DEFAULT TRUE,
    sort_order   SMALLINT    NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE brand IS
  'Telco retail brand. A natural-key primary key on purpose: exactly two rows, every '
  'referencing table names the column brand_code, and a UUID would make every PO join '
  'unreadable for no gain. NOT device_model.brand, which is the manufacturer.';

INSERT INTO brand (code, display_name, outlet_name, sort_order) VALUES
  ('IM3', 'IM3', 'Gerai IM3', 1),
  ('3ID', '3ID', '3Store',    2);

-- The MPX "Alamat Penerima" book. Every column is rendered by the Alamat Lengkap modal.
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
    -- A contact number, not an FWA MSISDN: stored as typed, same as customer.phone_number.
    CONSTRAINT ck_address_phone    CHECK (recipient_phone ~ '^(62|0)[0-9]{8,13}$'),
    CONSTRAINT ck_address_lat      CHECK (latitude  IS NULL OR latitude  BETWEEN -90  AND 90),
    CONSTRAINT ck_address_lng      CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180),
    -- Half a coordinate cannot be plotted, so it must not be storable.
    CONSTRAINT ck_address_geo_pair CHECK ((latitude IS NULL) = (longitude IS NULL))
);
-- One default delivery address per MPX. A partial unique index rather than a trigger, so
-- two concurrent "make this the default" writes cannot both win.
CREATE UNIQUE INDEX uq_address_one_default ON address (mpx_id) WHERE is_default;
CREATE INDEX ix_address_mpx ON address (mpx_id) WHERE is_active;


-- ---------------------------------------------------------------------
-- SUPPLY CHAIN — Shared write infrastructure
-- ---------------------------------------------------------------------

-- Replay cache for admin writes that create no single owning row. The AE side hangs an
-- idempotency_key column on each aggregate table, which works because each of those
-- writes creates exactly one row. Bulk MSISDN supply inserts N inventory rows, and
-- pairing creates none at all, so the key needs a home of its own — together with the
-- response it produced, because a retry must return the original answer rather than
-- re-run a 400-row insert to discover it already happened.
CREATE TABLE idempotency_record (
    idempotency_record_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_user_id UUID NOT NULL REFERENCES admin_user(admin_user_id) ON DELETE CASCADE,
    endpoint      VARCHAR(120) NOT NULL,
    key           UUID         NOT NULL,
    request_hash  TEXT         NOT NULL,   -- detects a key reused with new content
    response_body JSONB        NOT NULL,
    status_code   SMALLINT     NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_idem_admin_endpoint_key UNIQUE (admin_user_id, endpoint, key)
);

-- ---------------------------------------------------------------------
-- SUPPLY CHAIN — MODULE G — MSISDN procurement (DP -> IOH)
-- ---------------------------------------------------------------------

-- PO numbers come from a sequence so they are atomic under concurrency. A gap is
-- harmless; a duplicate is not.
CREATE SEQUENCE msisdn_po_seq START WITH 1;

CREATE TABLE msisdn_po (
    msisdn_po_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    po_code           VARCHAR(40)  NOT NULL,   -- 'ADVAN-20250523-207'
    device_partner_id UUID NOT NULL REFERENCES device_partner(device_partner_id),
    call_plan_id      UUID NOT NULL REFERENCES call_plan(call_plan_id),
    brand_code        VARCHAR(10) NOT NULL REFERENCES brand(code),
    qty_requested     INTEGER      NOT NULL,
    status            VARCHAR(20)  NOT NULL DEFAULT 'DIAJUKAN',
    note              TEXT,
    rejected_reason   TEXT,
    created_by_admin_id  UUID NOT NULL REFERENCES admin_user(admin_user_id),
    supplied_by_admin_id UUID REFERENCES admin_user(admin_user_id),
    submitted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_msisdn_po_code UNIQUE (po_code),
    CONSTRAINT ck_msisdn_po_status CHECK (status IN
        ('DIAJUKAN','DIPROSES','DITERIMA','DITOLAK','DIBATALKAN')),
    CONSTRAINT ck_msisdn_po_qty CHECK (qty_requested > 0),
    -- IOH has discretion to refuse (confirmed 2026-09-01), and a refusal the DP cannot
    -- act on is not a refusal. Required by the database, not merely by the schema.
    CONSTRAINT ck_msisdn_po_rejected CHECK (status <> 'DITOLAK' OR rejected_reason IS NOT NULL)
);
CREATE INDEX ix_msisdn_po_dp     ON msisdn_po (device_partner_id, submitted_at DESC);
CREATE INDEX ix_msisdn_po_status ON msisdn_po (status, submitted_at DESC);
COMMENT ON TABLE msisdn_po IS
  'A Device Partner request for MSISDNs. There is deliberately no item table: a supplied '
  'number IS an fwa_inventory row, carrying this msisdn_po_id. Two rows per number that '
  'must agree with each other forever is the failure this avoids.';

CREATE TABLE msisdn_po_status_history (
    history_id   BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    msisdn_po_id UUID NOT NULL REFERENCES msisdn_po(msisdn_po_id) ON DELETE CASCADE,
    old_status   VARCHAR(20),
    new_status   VARCHAR(20) NOT NULL,
    changed_by_admin_id UUID REFERENCES admin_user(admin_user_id),
    note         TEXT,
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_msisdn_po_hist ON msisdn_po_status_history (msisdn_po_id, changed_at);

-- ---------------------------------------------------------------------
-- SUPPLY CHAIN — MODULE I — Device PO (MPX -> DP)
-- ---------------------------------------------------------------------

CREATE SEQUENCE device_po_seq START WITH 1;

CREATE TABLE device_po (
    device_po_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    po_code           VARCHAR(80) NOT NULL,  -- 'PO-JAVA-3ID-RABIT CPE-R-28-20250809-407'
    mpx_id            UUID NOT NULL REFERENCES mpx(mpx_id),
    device_partner_id UUID NOT NULL REFERENCES device_partner(device_partner_id),
    device_model_id   UUID NOT NULL REFERENCES device_model(device_model_id),
    brand_code        VARCHAR(10) NOT NULL REFERENCES brand(code),
    qty               INTEGER NOT NULL,
    unit_price_idr    BIGINT  NOT NULL,      -- snapshot, never a join
    total_idr         BIGINT  NOT NULL,
    address_id        UUID NOT NULL REFERENCES address(address_id),
    pic_name          VARCHAR(150),
    pic_phone         VARCHAR(20),
    note              TEXT,
    rejected_reason   TEXT,
    status            VARCHAR(20) NOT NULL DEFAULT 'DIAJUKAN',
    created_by_admin_id  UUID NOT NULL REFERENCES admin_user(admin_user_id),
    accepted_by_admin_id UUID REFERENCES admin_user(admin_user_id),
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    accepted_at  TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_device_po_code UNIQUE (po_code),
    -- No DITERIMA_SEBAGIAN: confirmed 2026-09-01, MPX can only confirm receipt when
    -- every unit is present, so a short shipment stays at PERIKSA.
    CONSTRAINT ck_device_po_status CHECK (status IN
        ('DIAJUKAN','DIPROSES','DIKIRIM','PERIKSA','DITERIMA','DITOLAK','DIBATALKAN')),
    CONSTRAINT ck_device_po_qty   CHECK (qty > 0),
    CONSTRAINT ck_device_po_price CHECK (unit_price_idr > 0),
    -- Money is whole rupiah and the arithmetic is enforced here, so a client cannot
    -- submit a total that disagrees with its own line.
    CONSTRAINT ck_device_po_total CHECK (total_idr = unit_price_idr * qty),
    CONSTRAINT ck_device_po_phone CHECK (pic_phone IS NULL OR pic_phone ~ '^(62|0)[0-9]{8,13}$'),
    CONSTRAINT ck_device_po_rejected CHECK (status <> 'DITOLAK' OR rejected_reason IS NOT NULL)
);
CREATE INDEX ix_device_po_mpx    ON device_po (mpx_id, submitted_at DESC);
CREATE INDEX ix_device_po_dp     ON device_po (device_partner_id, submitted_at DESC);
CREATE INDEX ix_device_po_status ON device_po (status, submitted_at DESC);
COMMENT ON COLUMN device_po.unit_price_idr IS
  'Snapshot of device_model.list_price_idr at creation, in whole rupiah. Deliberately '
  'not a join: a later price change must never rewrite the value of a closed order.';

CREATE TABLE device_po_status_history (
    history_id   BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    device_po_id UUID NOT NULL REFERENCES device_po(device_po_id) ON DELETE CASCADE,
    old_status   VARCHAR(20),
    new_status   VARCHAR(20) NOT NULL,
    changed_by_admin_id UUID REFERENCES admin_user(admin_user_id),
    note         TEXT,                     -- 'J&T Express | JD0463672772'
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_device_po_hist ON device_po_status_history (device_po_id, changed_at);
COMMENT ON TABLE device_po_status_history IS
  'Backs the Riwayat panel of the Detail PO modal, which renders (tanggal, status, oleh, '
  'keterangan) directly from these rows. Trustworthy only because every transition writes '
  'here in the same transaction as the status change.';

-- The two foreign keys deferred by the fwa_inventory migration, added once their target
-- tables exist (migrations 0006 and 0007).
ALTER TABLE fwa_inventory
    ADD CONSTRAINT fk_inv_msisdn_po FOREIGN KEY (msisdn_po_id) REFERENCES msisdn_po(msisdn_po_id),
    ADD CONSTRAINT fk_inv_device_po FOREIGN KEY (device_po_id) REFERENCES device_po(device_po_id);


-- ---------------------------------------------------------------------
-- SUPPLY CHAIN — MODULE I(b) — Shipment and goods receipt
-- ---------------------------------------------------------------------

-- One dispatch per order. Safe because reshipping is handled off-system: an order goes
-- out once, and a short delivery is corrected outside this system before the MPX
-- confirms receipt.
CREATE TABLE shipment (
    shipment_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_po_id UUID NOT NULL REFERENCES device_po(device_po_id) ON DELETE CASCADE,
    courier_name VARCHAR(60) NOT NULL,   -- free text; couriers are manual for v1
    awb          VARCHAR(60) NOT NULL,   -- Nomor Resi, the proof of dispatch
    shipped_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    estimated_delivery_date DATE,
    delivered_at TIMESTAMPTZ,
    created_by_admin_id UUID REFERENCES admin_user(admin_user_id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_shipment_device_po UNIQUE (device_po_id),
    CONSTRAINT ck_shipment_awb     CHECK (length(btrim(awb)) > 0),
    CONSTRAINT ck_shipment_courier CHECK (length(btrim(courier_name)) > 0),
    CONSTRAINT ck_shipment_eta     CHECK
        (estimated_delivery_date IS NULL OR estimated_delivery_date >= shipped_at::date)
);
COMMENT ON COLUMN shipment.awb IS
  'Nomor Resi. Written into device_po_status_history as "{courier} | {awb}", which is '
  'what the Riwayat panel renders.';

CREATE TABLE shipment_milestone (
    milestone_id BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    shipment_id  UUID NOT NULL REFERENCES shipment(shipment_id) ON DELETE CASCADE,
    milestone    VARCHAR(20) NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    note         TEXT,
    created_by_admin_id UUID REFERENCES admin_user(admin_user_id),
    CONSTRAINT ck_milestone CHECK (milestone IN
        ('SHIPPED','IN_TRANSIT','OUT_FOR_DELIVERY','DELIVERED')),
    -- Each step appears once, or the tracker draws a bar that goes backwards.
    CONSTRAINT uq_milestone_per_shipment UNIQUE (shipment_id, milestone)
);
CREATE INDEX ix_milestone_shipment ON shipment_milestone (shipment_id, occurred_at);

-- One row per completed receipt, and only ever a complete one. Confirmed 2026-09-01:
-- MPX can confirm only when every unit is physically present; a short box is resolved
-- outside this system. Hence no line table, no per-unit condition, no partial flag —
-- which units were received is exactly the fwa_inventory rows carrying this order id.
CREATE TABLE goods_receipt (
    goods_receipt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_po_id UUID NOT NULL REFERENCES device_po(device_po_id) ON DELETE CASCADE,
    received_by_admin_id UUID NOT NULL REFERENCES admin_user(admin_user_id),
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    qty_received INTEGER NOT NULL,
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_goods_receipt_device_po UNIQUE (device_po_id),
    CONSTRAINT ck_goods_receipt_qty CHECK (qty_received > 0)
);

-- ---------------------------------------------------------------------
-- SUPPLY CHAIN — MODULE J — Allocation to a salesman
--
-- THE SEAM. Everything above moves stock between three companies with no
-- effect on the mobile app. The allocation endpoint sets
-- fwa_inventory.allocated_ae_id, and only then can an AE's barcode scanner
-- resolve the number. That column has exactly one writer.
-- ---------------------------------------------------------------------

CREATE TABLE stock_allocation (
    stock_allocation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mpx_id UUID NOT NULL REFERENCES mpx(mpx_id),
    ae_id  UUID NOT NULL REFERENCES account_executive(ae_id),
    allocated_by_admin_id UUID NOT NULL REFERENCES admin_user(admin_user_id),
    allocated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    mode   VARCHAR(10) NOT NULL,   -- 'AUTO' (FIFO on received_at) | 'MANUAL'
    qty    INTEGER     NOT NULL,
    note   TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_allocation_mode CHECK (mode IN ('AUTO','MANUAL')),
    CONSTRAINT ck_allocation_qty  CHECK (qty > 0)
);
CREATE INDEX ix_allocation_mpx ON stock_allocation (mpx_id, allocated_at DESC);
CREATE INDEX ix_allocation_ae  ON stock_allocation (ae_id,  allocated_at DESC);

CREATE TABLE stock_allocation_item (
    stock_allocation_id UUID NOT NULL
        REFERENCES stock_allocation(stock_allocation_id) ON DELETE CASCADE,
    msisdn VARCHAR(15) NOT NULL REFERENCES fwa_inventory(msisdn),
    PRIMARY KEY (stock_allocation_id, msisdn),
    -- A unit is allocated once, ever. NOTE: this means a unit an AE returns cannot be
    -- re-allocated without relaxing the constraint. There is no return-to-stock flow in
    -- v1; if one is built, scope this uniqueness to live allocations rather than drop it.
    CONSTRAINT uq_allocation_item_msisdn UNIQUE (msisdn)
);
COMMENT ON CONSTRAINT uq_allocation_item_msisdn ON stock_allocation_item IS
  'A physical unit cannot be in two salesmen''s hands. The FOR UPDATE SKIP LOCKED in the '
  'allocation service is what makes concurrent allocation fail cleanly; this is the '
  'backstop that makes it impossible.';


-- ---------------------------------------------------------------------
-- Optional: enable when the AI Sales Hotspot work starts
-- ---------------------------------------------------------------------
-- CREATE EXTENSION IF NOT EXISTS postgis;
-- ALTER TABLE customer   ADD COLUMN geom geography(Point,4326)
--   GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude),4326)::geography) STORED;
-- CREATE INDEX ix_cust_geom ON customer USING GIST (geom);
