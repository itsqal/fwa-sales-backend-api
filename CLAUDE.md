# CLAUDE.md

Guidance for AI coding agents working on this repository.
Read this file completely before writing code. If a request conflicts with anything here, say so rather than silently working around it.

---

## 1. What this is

The backend for the **Account Executive (AE) module** of the HiFi AIR Sales Management Platform — the FWA (Fixed Wireless Access) sales digitalization programme at Indosat Ooredoo Hutchison.

It serves an Android app used by field sales staff doing door-to-door selling. An AE registers prospects they meet, then activates a HiFi AIR unit for them by scanning the MSISDN barcode on the box.

**The five screens this API exists to serve:**

| Screen | What it does |
|---|---|
| Home | Daily activity chart, Total Aktivasi and Insentif tiles |
| Input New Customer | Register a prospect: name, phone, address, GPS, interaction status |
| Aktivasi Pelanggan | Scan MSISDN → server resolves IMEI + modem → submit activation |
| Daftar New Customer / Hot Leads / Aktivasi | Filtered history lists |
| Report | Four counter tiles + navigation into the lists |

### Scope boundary — enforce this

This backend covers **AE activity only**.

**Out of scope. Do not build, and push back if asked:**

- GSE / DSE / DSF flows of any kind
- Open New Outlet (ONO) and Gadget Store sell-in
- The MPX → salesman stock request / allocation / AWB workflow (AE stock is pre-allocated)
- AI Customer Profiling and Sales Hotspot Analytics (deferred; the schema is designed to accept them additively)

If a task appears to require one of these, stop and ask. Scope creep here is expensive because those flows have different actors and approval chains.

---

## 2. Golden rules

These are invariants, not preferences. Violating one is a bug even if tests pass.

1. **`openapi.yaml` is the contract of record.** It was hand-written and reviewed. Do not regenerate it from code. If an implementation needs to diverge, change the spec first in the same commit and say why.

2. **AE identity always comes from the JWT.** No endpoint accepts `aeId` in a request body or query param. A client must never be able to write on another AE's behalf. Every list query is implicitly scoped to the authenticated AE.

3. **The client never asserts IMEI or modem type.** `POST /activations` receives a customer ID, a scanned MSISDN, and a GPS fix. The server resolves ICCID / IMEI / device model from `fwa_inventory` and snapshots them onto the activation row. Trusting client-supplied IMEI would let an AE fabricate inventory.

4. **`activation.activation_date` is written only by the GA feed.** It arrives via `POST /internal/ga-events` from GCP. No AE-facing code path may set it. It is the difference between the green *Activated* and red *Not Activated* badge, and it gates incentive.

5. **Normalise MSISDNs to `62` form at the edge.** A barcode may scan as `085882724305`; it is stored and compared as `6285882724305`. Normalise once, in the request layer, never deeper. A `CHECK` constraint enforces `^62[0-9]{8,13}$` so mistakes fail loudly.

6. **The server is authoritative on time.** Accept `deviceTime` for audit, never for business logic. Field phones have wrong clocks.

7. **`is_mocked` is flagged, never auto-rejecting.** Fake GPS is a genuine fraud vector, but a real AE hitting a device quirk must not be blocked in the field. Record it, surface it for supervisor review.

8. **Every creating `POST` honours `Idempotency-Key`.** The mobile app queues writes offline and retries. Replaying a key with the same payload returns the original resource. Without this, one dropped response creates a duplicate activation *and* a duplicate incentive payment.

9. **Interaction status is `EDUKASI` / `HOT_LEADS` / `PURCHASE`.** Some project documents say Potential / Sell In / Non-Potential for the same concept. The i-Sales taxonomy above is authoritative. Do not introduce the other one.

10. **Money is an integer of whole rupiah.** Never float. The UI divides by 100 000 to render "Insentif 242" — that scaling is presentation-only and lives in the client.

11. **Constraints belong in the database, not only in Python.** Uniqueness, enum membership, and referential integrity are enforced by PostgreSQL so they hold under concurrency. Application validation is for good error messages, not for correctness.

12. **Do not invent business rules.** Incentive amounts, geofence radius, and activation limits per customer are genuinely undecided (see §10). If code needs one, ask — do not pick a plausible number and bury it in a constant.

---

## 3. Stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| Framework | FastAPI (async) |
| ORM | SQLAlchemy 2.0, async, declarative mapped classes |
| Driver | asyncpg |
| Validation | Pydantic v2 |
| Migrations | Alembic |
| Database | PostgreSQL 16 |
| Auth | JWT access + rotating refresh, argon2id password hashing |
| Package manager | uv |
| Lint / format | ruff |
| Types | mypy (strict on `app/`) |
| Tests | pytest + pytest-asyncio + httpx `AsyncClient` |
| Contract tests | schemathesis against `openapi.yaml` |
| Deployment | Docker on a cloud VM, behind nginx |

**Do not add dependencies without asking.** Prefer the standard library, then the stack above. Every new package is a supply-chain and maintenance cost on a project with one developer.

**Back-office admin** (the Admin / Sales Team Leader dashboard in the Solution Description) is a **separate future concern**. Do not bolt admin CRUD onto this API. When it happens it will likely be SQLAdmin against the same database.

---

## 4. Repository layout

```
app/
  main.py                 FastAPI app factory, middleware, exception handlers
  core/
    config.py             pydantic-settings, env-driven. No literals.
    security.py           JWT encode/decode, password hashing
    deps.py               get_current_ae, get_db — shared dependencies
    errors.py             AppError hierarchy + the single error envelope
    pagination.py         page/perPage → LIMIT/OFFSET + meta block
  db/
    session.py            async engine + sessionmaker
    base.py               DeclarativeBase
  models/                 SQLAlchemy models, one module per aggregate
  schemas/                Pydantic request/response models
  api/v1/                 Routers, one per OpenAPI tag
  services/               Business logic. Routers stay thin.
alembic/
  versions/
tests/
  conftest.py
  test_*.py
docs/
  openapi.yaml            THE CONTRACT
  schema.sql              Reference DDL — the model of record
  AE_Data_Model.md        Design rationale and UI traceability
  erd.png
docker-compose.yml
Dockerfile
pyproject.toml
```

**Layering rule:** routers parse and authorise; services hold business logic; models hold persistence. A router must not contain a query. A service must not import FastAPI.

---

## 5. Commands

```bash
# setup
uv sync
cp .env.example .env
docker compose up -d db

# database
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "add x"   # ALWAYS review the generated file
uv run python -m app.scripts.seed                   # dev fixtures

# run
uv run uvicorn app.main:app --reload

# quality — all four must pass before you call work done
uv run ruff format .
uv run ruff check --fix .
uv run mypy app
uv run pytest
```

---

## 6. Contract-first workflow

`docs/openapi.yaml` came first and the database was designed to serve specific screens. Preserve that direction.

**To add or change an endpoint:**

1. Edit `docs/openapi.yaml`.
2. Validate: `uv run python -c "import yaml;from openapi_spec_validator import validate;validate(yaml.safe_load(open('docs/openapi.yaml')))"`
3. Write the Pydantic schemas to match — field names, optionality, and enum values exactly.
4. Implement the service, then the router.
5. Add tests, including the error paths the spec declares.

FastAPI generates its own `/openapi.json`. That is a **convenience for Swagger UI, not the contract.** Where the two disagree, `docs/openapi.yaml` wins and the code is wrong.

### API conventions

- JSON bodies are `camelCase`; Python and the database are `snake_case`. Convert in Pydantic with `alias_generator=to_camel` and `populate_by_name=True`. Do not hand-write conversions.
- Every error response is `{"error": {"code", "message", "details"}}`. `code` is a stable `SCREAMING_SNAKE` identifier and is never localised or reworded — clients branch on it. `message` is human-readable and safe to show an AE.
- Lists return `{"data": [...], "meta": {page, perPage, total, totalPages}}`.
- `404` is returned for a resource that exists but belongs to another AE. Never `403` — that confirms the record exists.

---

## 7. Database rules

- **`docs/schema.sql` is the model of record.** It has been applied to a live PostgreSQL 16 and tested against real sample data. SQLAlchemy models must match it — column names, types, nullability, constraints.
- **Schema changes go through Alembic.** Never hand-edit the database. Always read an autogenerated migration before applying it: autogenerate misses `CHECK` constraints, partial indexes, functional indexes, and functions, all of which this schema uses.
- **Keep `schema.sql` in sync** when you add a migration. It is what a new developer reads first.
- **Never `DROP` or destructively `ALTER` without asking.** There is no automated backup on the dev VM.
- Use `session.begin()` for transactions. One transaction per request, committed by the dependency, not scattered through services.
- Watch for N+1: use `selectinload` for collections, `joinedload` for many-to-one.

### Things in this schema that autogenerate will not see

- `UNIQUE INDEX uq_ae_code_ci ON account_executive (upper(ae_code))` — login is case-insensitive
- All `CHECK` constraints, including `ck_act_ga` (an `ACTIVATED` row must carry an `activation_date`)
- `fn_ae_daily_activity(ae_id, from, to)` — the home chart series
- Identity sequences starting at 1000000000 (customer) and 2900000000 (activation), which make the 10-digit IDs the UI displays real

---

## 8. Testing

**Test against real PostgreSQL. Never SQLite.** The schema depends on `CHECK` constraints, functional indexes, identity columns, `JSONB`, and a SQL function. SQLite silently accepts data PostgreSQL rejects, so a green SQLite suite proves nothing.

Use the `docker compose` database, or testcontainers. Each test runs in a transaction that is rolled back.

**Every endpoint needs:** the happy path, auth rejection, validation failure, and each declared error code.

**Always test these specifically — they are the ones that will actually break:**

- Activating the same MSISDN twice → `409`
- Activating an MSISDN allocated to a different AE → `422 MSISDN_NOT_ALLOCATED`
- Reading or writing another AE's customer → `404`
- Replaying an `Idempotency-Key` → the original resource, no duplicate row
- A GA event for an unknown MSISDN → counted as unmatched, no crash
- Date-range reports that span zero-activity days → an unbroken series, no gaps

Run `schemathesis run docs/openapi.yaml --base-url http://localhost:8000` before declaring an endpoint finished.

---

## 9. Security

- Never log credentials, tokens, full MSISDNs, or IMEIs. Mask to last 4 digits.
- Passwords are argon2id. HQ issues an initial password; `must_change_pw` forces a reset on first login.
- Access tokens are short-lived (15 min); refresh tokens rotate and are stored hashed and revocable per device.
- `POST /internal/ga-events` uses a service credential and must be unreachable from the public app path. It is not an AE-token endpoint.
- Rate-limit `/auth/login` — AE codes are guessable by design (`AE-BENGKULU1`).
- All configuration comes from environment variables. No secret ever enters the repository. Keep `.env.example` current.
- Uploaded selfies and documents are private. Serve them through short-lived signed URLs, never a public bucket path.
- This system holds customer names, phone numbers, home addresses, and GPS coordinates. Treat it as personal data: no unnecessary retention, no bulk export endpoints, no PII in error messages.

---

## 10. Domain glossary

Field terminology is Indonesian and telco-specific. Do not guess at these.

| Term | Meaning |
|---|---|
| **FWA** | Fixed Wireless Access — home internet over the mobile network |
| **HiFi AIR** | The FWA product being sold |
| **AE** | Account Executive — field sales, door-to-door to end customers. **The only user of this backend.** |
| **GSE** | Gadget Store Executive — sells through gadget stores. Out of scope. |
| **DSE / DSF** | Other salesman types. Out of scope. |
| **MPX / SDP** | Distributor / stock point that allocates devices to salesmen |
| **ONO** | Open New Outlet — registering a new gadget store. GSE only, out of scope. |
| **PJP** | *Pola Jalur Permanen* — the planned visit route. For an AE this is the customer list itself, not a separate entity. |
| **CICO** | Check-In / Check-Out — daily attendance, selfie plus GPS |
| **Absensi** | Attendance |
| **Sell In** | Recording a sold unit. For an AE this is **Aktivasi Pelanggan** — customer activation. |
| **GA / Gross Add** | A confirmed new activation on the network. `GA Date` comes from GCP and is what flips a record to *Activated*. |
| **MSISDN** | The phone number. Format `62…`. |
| **ICCID** | SIM card serial number |
| **IMEI** | Device hardware serial. Bundled to an MSISDN by the Device Partner. |
| **Tipe Modem** | CPE model, e.g. `HKM 127+` |
| **Edukasi** | Customer was educated but has no purchase intent yet |
| **Hot Leads** | High intent, needs follow-up |
| **Purchase** | Agreed to buy |
| **Insentif** | AE commission, accrued on confirmed activations |
| **Tanggal Pergi** | The date the AE made the visit |
| **Longlat** | Longitude/latitude pair |
| **Daftar** | "List of" — as in *Daftar Aktivasi*, the activation list screen |

---

## 11. Open decisions — ask, do not assume

These are genuinely unresolved. If your task depends on one, stop and ask.

1. **Incentive rules.** Amounts and conditions are unspecified. Evidence suggests tiering on confirmed GA rather than on submissions, but this is unconfirmed for AE.
2. **Geofence radius for CICO.** The out-of-range approval workflow is modelled; the tolerance in metres is not set.
3. **Multiple activations per customer.** Currently allowed — one row per unit. If a household must be limited to one, that is a new constraint.
4. **GA feed direction.** The contract assumes GCP pushes to `POST /internal/ga-events`. If we must poll instead, that is a scheduled job, not an endpoint.
5. **Biometric registration.** The source brief has a section but no data entity. It is a privacy question before it is a schema question.

---

## 12. Working style

- **Read `docs/AE_Data_Model.md` before touching the schema.** It explains why things are shaped as they are, including reconciliations between conflicting source documents. Several designs that look redundant are load-bearing.
- Make the smallest change that fully solves the problem.
- Prefer clarity to cleverness. This codebase is maintained by one person who is not primarily a backend developer.
- Comment *why*, never *what*. The domain is unusual; the Python is not.
- When you find a bug outside your task, report it rather than fixing it silently.
- If a requirement is ambiguous, ask one specific question instead of building both options.
- State assumptions explicitly in your summary when you had to make one.

### Do not

- Regenerate `openapi.yaml` from code
- Add an `aeId` parameter to any AE-facing endpoint
- Set `activation_date` outside the GA ingestion path
- Use SQLite for tests
- Introduce the Potential / Sell In / Non-Potential enum
- Store money as a float
- Build GSE, ONO, or stock-allocation features
- Add a dependency without asking
- Commit a `.env` file or any secret
