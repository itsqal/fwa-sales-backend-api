# Mobile app integration notes — read before changing the Flutter client

**Audience:** whoever (human or AI agent) is building the HiFi AIR **Account Executive
Android app** against this backend.
**Written:** 2026-09-01, at the end of the supply-chain backend extension.

---

## 1. The short version

A large piece of work landed on this backend: a whole **supply chain module** for a web
dashboard used by Device Partners, IOH and MPX — 10 migrations, 8 new tables, 45 new
endpoints.

**None of it changes anything the mobile app calls.**

You do not need to change the app. You do not need to regenerate models. Nothing you
parse has a new field, a removed field, or a changed type.

If you only read one section, read this one and stop.

---

## 2. What "unchanged" means precisely, and how it was checked

Claims like this are cheap, so here is the evidence.

### 2.1 No AE endpoint was added, removed, or renamed

The contract had **21 AE-facing paths** before the work and **21 after**. Nothing added,
nothing removed.

### 2.2 Every response schema you parse is byte-identical

Compared against the contract as it stood before the work began:

| Schema | Result |
|---|---|
| `AccountExecutive` | identical |
| `AuthTokens` | identical |
| `InventoryItem` | identical |
| `Customer` | identical |
| `Activation` | identical |
| `Attendance` | identical |

No field added, removed, retyped, or made nullable.

### 2.3 The only contract edits on AE operations are documentation

Twenty-four differences show up in a diff, and every one is a response code that the API
**could always return** but had never been written down:

* `401` added to 14 operations — they always answered 401 without a token; the contract
  simply never said so.
* `400` added to every operation with a body — the ASGI layer rejects a body that is not
  valid UTF-8 before validation runs. Pre-existing behaviour, now documented.
* `POST /auth/login` and `POST /auth/refresh` gained `minLength: 1` on their fields. The
  API already rejected an empty `aeCode`, `password`, or `refreshToken`; the contract now
  declares what the server was already doing.

**If your client generates code from `openapi.yaml`, regenerating is safe and changes
nothing structural.** You may gain a couple of documented error cases. That is all.

### 2.4 The one behavioural change, and why it is a no-op for you

Three endpoints — `GET /inventory/me`, `GET /inventory/msisdn/{msisdn}` and
`GET /sync/bootstrap` — now filter inventory by status.

That sounds alarming and is not. `fwa_inventory` used to hold only units that belonged to
a salesman. It now also holds units moving through the supply chain: numbers IOH has
issued but nobody has paired, bundles in transit to a warehouse, stock received but not
yet handed out. The filter exists so none of that leaks into the app.

The set of statuses the app can see is **exactly the six that existed before this work**:

```
before:  ACTIVATED, ALLOCATED, AVAILABLE, BLOCKED, CONSUMED, RETURNED
now:     ACTIVATED, ALLOCATED, AVAILABLE, BLOCKED, CONSUMED, RETURNED

hidden that you could see before:  none
newly visible:                     none
```

Verified programmatically, not by inspection. Your result set is unchanged.

---

## 3. What actually changed for you, in practice

One thing, and it is operational rather than technical.

**Stock now arrives through a real process instead of being inserted by hand.**

Previously somebody ran SQL to put rows in `fwa_inventory` and set `allocated_ae_id`.
Now a unit reaches a salesman like this:

```
DP requests numbers  →  IOH supplies them  →  DP pairs MSISDN + IMEI
   →  MPX orders devices  →  DP ships (AWB)  →  MPX confirms receipt
      →  MPX allocates to an AE   ←── the app can see it from here
```

The practical consequence: **an AE's stock appears when an MPX admin allocates it**, not
before. If a tester says "my stock list is empty", the answer is almost always that
nobody has allocated to them yet — not that the app is broken.

`POST /admin/allocations` is the only thing in the entire system that writes
`allocated_ae_id`.

---

## 4. Rules that still hold, and matter to you

These were true before and are still true. The supply chain work was built specifically
so they stay true.

### 4.1 AE identity comes from the JWT, always

No AE endpoint accepts an `aeId` in a body, query, or path. There is no way for the app
to read or write on another AE's behalf, and no parameter to add. Every list is scoped to
the token automatically.

The dashboard *does* have endpoints that name an `aeId` — an MPX has to say who it is
allocating stock to. Those live under `/admin`, require a different token audience, and
are unreachable from the app. There is an automated test asserting that any endpoint
naming an `aeId` sits behind the admin audience.

### 4.2 Your token will never work on `/admin`, and that is deliberate

Admin tokens carry an `aud: "admin"` claim. AE tokens carry none. Presenting an AE token
to `/admin` returns `401 INVALID_TOKEN`; presenting an admin token to an AE route does
the same. Enforced at the JWT layer, before any handler runs.

**Do not try to call `/admin/*` from the app.** It is a different product for a different
audience, and it will not work.

### 4.3 The client never asserts IMEI or modem type

Still true. `POST /activations` takes a customer id, a scanned MSISDN and a GPS fix. The
server resolves ICCID, IMEI and device model from inventory and snapshots them onto the
activation. Do not send them; they will be ignored.

### 4.4 MSISDNs are normalised to `62` form

Still true. A barcode scanning as `085882724305` must be sent as `6285882724305`.

### 4.5 `Idempotency-Key` on every creating POST

Still true, and still the thing that makes the offline outbox safe. Unchanged.

---

## 5. Things that are still missing, and will affect you eventually

Honest list of gaps, so they are not a surprise.

**There is no password-change endpoint.** The API reports `mustChangePassword` at login
but exposes no way to act on it — for AEs *or* admins. This predates the supply-chain
work and is still open. Until it exists, do not build a "change your password" screen
expecting a backend for it, and treat `mustChangePassword: true` as informational.
Password resets are an out-of-band HQ task.

**`PATCH /customers/lookup` answers 401 rather than 405.** A quirk of route matching plus
auth running before path validation. Harmless, but if a fuzzer flags it, it is known and
deliberate — answering 401 before revealing whether a method exists is the more
conservative behaviour.

**Incentive rules are only half decided.** The 4G/5G activation tiers are live
(Rp 35.000 / Rp 135.000). Volume tiering, targets, and whether registering a customer or
a hot lead earns anything are all still open. Do not render a total that implies a rule
nobody has agreed.

---

## 6. If you are an AI agent working on the Flutter app

Concretely:

* **Do not change the app in response to this backend work.** There is nothing to
  integrate. If a task says "integrate the supply chain changes", the correct answer is
  that the AE contract is unchanged and no client work is required.
* **Read `docs/openapi.yaml` for the AE endpoints only** — everything not under `/admin`.
  The `/admin` half is a separate product for the web dashboard.
* **Do not add an `aeId` parameter to anything.** It is the one rule most likely to be
  broken by accident, and there is a test that will catch it.
* **If stock is missing in testing, check allocation first.** Ask whether an MPX admin has
  allocated units to that AE. It is far more often that than a bug.
* **Regenerating client models from the contract is safe.** No schema changed.

### Testing against a local backend

```bash
docker compose up -d db
uv run alembic upgrade head
uv run python -m app.scripts.seed        # dev fixtures, incl. AE-BENGKULU1
uv run uvicorn app.main:app --reload
```

Two Postman collections are in `postman/`:

* `FWA_AE_API.postman_collection.json` — the AE endpoints your app calls.
* `FWA_Supply_Chain_API.postman_collection.json` — the dashboard. Its **folder 09** logs
  in as an AE and proves the mobile-facing endpoints see stock the dashboard just
  allocated. Run that collection top to bottom and you have a salesman with real stock to
  test against, without writing any SQL.
