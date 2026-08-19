# FWA Sales Digitalization — Account Executive API

Backend for the **Account Executive (AE)** module of the HiFi AIR Sales Management
Platform (Indosat Ooredoo Hutchison). It serves the Android app used by field sales
staff doing door-to-door selling: register a prospect, scan the MSISDN barcode on the
HiFi AIR box, activate, and track performance.

Scope is AE activity only. GSE / DSE / DSF flows, Open New Outlet, and the MPX stock
request workflow are deliberately out of scope — see `CLAUDE.md` §1.

## Sources of truth

| File | Role |
|---|---|
| `docs/openapi.yaml` | **The contract.** Hand-written and reviewed. Code follows it, never the reverse. |
| `docs/schema.sql` | **The model of record.** SQLAlchemy models and migrations must match it. |
| `docs/erd.png` | Entity-relationship diagram. |
| `CLAUDE.md` | Engineering rules and domain glossary. |

`references/` holds the original drop of those documents, kept untouched for
provenance. Work against `docs/`.

## Setup

```bash
uv sync --extra dev
cp .env.example .env          # then fill in JWT_SECRET, SERVICE_API_KEY, FILE_URL_SECRET
docker compose up -d db       # or point DATABASE_URL at an existing PostgreSQL 16
uv run alembic upgrade head
uv run python -m app.scripts.seed
uv run uvicorn app.main:app --reload
```

Swagger UI is at <http://localhost:8000/docs>. It is generated from the code and is a
convenience only — where it disagrees with `docs/openapi.yaml`, the code is wrong.

## Quality gates

All four must pass before work is called done:

```bash
uv run ruff format .
uv run ruff check --fix .
uv run mypy app
uv run pytest
```

Tests run against **real PostgreSQL**, never SQLite — the schema leans on `CHECK`
constraints, functional indexes, identity columns and a SQL function that SQLite does
not enforce. Point `TEST_DATABASE_URL` at a scratch database; each test runs inside a
transaction that is rolled back.

Contract fuzzing:

```bash
schemathesis run docs/openapi.yaml --base-url http://localhost:8000/v1
```

## Design notes that are easy to get wrong

- **AE identity comes from the JWT.** No endpoint accepts `aeId`. Every list query is
  implicitly scoped to the authenticated AE, and a record belonging to another AE
  returns `404`, never `403`.
- **The client never asserts IMEI or modem type.** `POST /activations` takes a customer,
  a scanned MSISDN and a GPS fix; the server resolves the device from `fwa_inventory`
  and snapshots it onto the activation row.
- **`activation_date` is written only by the GA feed** (`POST /internal/ga-events`).
  It is the difference between the green *Activated* and red *Not Activated* badge.
- **MSISDNs are normalised to `62` form in the request layer**, once, at the edge.
- **`is_mocked` is flagged, never auto-rejecting.** A real AE hitting a GPS quirk must
  not be blocked in the field.
- **Every creating `POST` honours `Idempotency-Key`**, because the mobile app queues
  writes offline and retries.
- **Money is an integer of whole rupiah.** The `÷100 000` that renders "Insentif 242"
  is presentation-only and lives in the client.

## Undecided business rules

These are unresolved and the code does **not** guess at them (`CLAUDE.md` §11):

- **Incentive amounts and conditions.** The accrual engine is data-driven: it reads
  `incentive_rule` rows and writes `incentive_ledger` entries when the GA feed confirms
  an activation. No rule rows are seeded, so `incentiveIdr` is `0` until the business
  supplies the numbers. Nothing is hardcoded.
- **Geofence radius for CICO.** `GEOFENCE_RADIUS_M` is empty by default. While unset,
  `withinGeofence` is `null` and check-ins are `AUTO_APPROVED`; set the env var to
  switch on the out-of-range approval workflow.
- **Multiple activations per customer** are currently allowed — one row per unit.
