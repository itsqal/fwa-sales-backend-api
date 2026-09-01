"""Supply chain step 4 — fwa_inventory becomes the full unit lifecycle record.

**This is the only migration in the programme that can take the field force offline.**
Read this docstring before changing anything here.

``fwa_inventory`` is currently the AE barcode scanner's lookup table: every activation
resolves through it. This migration turns it into the record of a unit's whole journey,
from an MSISDN IOH has issued but nobody has paired, to a device allocated to a
salesman. The dangerous part is that a unit now exists in states the mobile app has
never seen, and — for the first time — with ``imei IS NULL``.

Why the schema half is safe
---------------------------
* ``ADD COLUMN`` with no default is metadata-only on PostgreSQL 11+. No rewrite.
* ``DROP NOT NULL`` is metadata-only. No rewrite.
* The status enum is ``VARCHAR`` + ``CHECK``, not a native enum, so widening it is a
  constraint swap rather than the table rewrite the spec feared.
* Both CHECKs are added ``NOT VALID`` and then ``VALIDATE``d. Adding a validated CHECK
  takes ACCESS EXCLUSIVE for the length of a full scan; ``VALIDATE CONSTRAINT`` takes
  only SHARE UPDATE EXCLUSIVE, so reads and writes continue. Every existing row already
  satisfies both predicates, but the two-step form means the lock is short regardless
  of how large the table has grown.
* ``ck_inv_imei`` and ``uq_inv_imei`` are deliberately left alone. Verified against
  PostgreSQL 16: a regex CHECK evaluates to UNKNOWN on NULL and a CHECK passes on
  UNKNOWN, and a UNIQUE constraint permits many NULLs. So unpaired rows neither fail
  the format check nor collide with one another.

Why the application half is the real work
-----------------------------------------
The compatibility filter shipped alongside this migration, in the same commit, is not
tidying — it is the reason the migration is safe. ``lookup_msisdn`` fetches a row by
MSISDN with **no ownership filter**, so the moment IOH supplies a batch it would resolve
an unpaired number and try to serialise ``imei=None`` into a field the AE contract types
as a required string. That is a 500 on the barcode scanner, on a number the AE has every
reason to scan.

``list_my_inventory`` and ``fetch_allocated_stock`` both filter on ``allocated_ae_id``,
which is NULL for supply-chain rows, so neither can crash today. They get the status
filter anyway: nothing at the database level stops ``allocated_ae_id`` being set on a
row that has not been received, and an AE must not see stock that is still in transit.

Two foreign keys are deliberately deferred
------------------------------------------
``msisdn_po_id`` and ``device_po_id`` are added here as plain UUIDs because
``msisdn_po`` and ``device_po`` do not exist until steps 5 and 7. The columns are added
now so this table's shape is settled in one pass against the live app; the FK
constraints are added by those later migrations, which is the point at which they can
be enforced. Adding a nullable column later would be metadata-only too — the choice is
about touching this table once, not about lock duration.

Revision ID: 0005_fwa_inventory_lifecycle
Revises: 0004_commercial_master
Create Date: 2026-09-01

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_fwa_inventory_lifecycle"
down_revision: str | None = "0004_commercial_master"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# AVAILABLE and CONSUMED are retained but deprecated: AVAILABLE predates allocation
# being explicit, and CONSUMED is now written only by the activation path. They are not
# dropped because removing a value would mean rewriting the CHECK against live rows that
# still carry them.
ALL_STATUSES = (
    "'AVAILABLE','ALLOCATED','CONSUMED','ACTIVATED','RETURNED','BLOCKED',"
    "'MSISDN_ISSUED','PAIRED','ASSIGNED','SHIPPED','RECEIVED'"
)


def upgrade() -> None:
    # --- 1. New columns. Metadata-only on PG 11+; no rewrite, no lock of consequence.
    op.execute(
        """
        ALTER TABLE fwa_inventory
            ADD COLUMN msisdn_po_id       UUID,
            ADD COLUMN device_po_id       UUID,
            ADD COLUMN mpx_id             UUID REFERENCES mpx(mpx_id),
            ADD COLUMN brand_code         VARCHAR(10) REFERENCES brand(code),
            ADD COLUMN call_plan_id       UUID REFERENCES call_plan(call_plan_id),
            ADD COLUMN paired_at          TIMESTAMPTZ,
            ADD COLUMN paired_by_admin_id UUID REFERENCES admin_user(admin_user_id),
            ADD COLUMN received_at        TIMESTAMPTZ
        """
    )
    op.execute(
        "COMMENT ON COLUMN fwa_inventory.msisdn_po_id IS "
        "'The IOH supply request this number came from. No FK yet — msisdn_po arrives "
        "in migration 0006, which adds the constraint.'"
    )
    op.execute(
        "COMMENT ON COLUMN fwa_inventory.device_po_id IS "
        "'The MPX order this bundle was attached to. No FK yet — device_po arrives in "
        "migration 0008, which adds the constraint.'"
    )
    op.execute(
        "COMMENT ON COLUMN fwa_inventory.received_at IS "
        "'When MPX confirmed physical receipt. This is the FIFO key for automatic "
        "stock allocation, which is why it is a column and not derived from history.'"
    )

    # --- 2. Relax imei. Metadata-only. ck_inv_imei and uq_inv_imei both tolerate NULL,
    #        so neither is touched.
    op.execute("ALTER TABLE fwa_inventory ALTER COLUMN imei DROP NOT NULL")

    # --- 3. Widen the status vocabulary. Drop-and-add on a VARCHAR CHECK, not an enum
    #        alter, so there is no table rewrite. NOT VALID first to keep the exclusive
    #        lock to a metadata update, then VALIDATE under a weaker lock.
    op.execute("ALTER TABLE fwa_inventory DROP CONSTRAINT ck_inv_status")
    op.execute(
        f"ALTER TABLE fwa_inventory ADD CONSTRAINT ck_inv_status "
        f"CHECK (status IN ({ALL_STATUSES})) NOT VALID"
    )
    op.execute("ALTER TABLE fwa_inventory VALIDATE CONSTRAINT ck_inv_status")

    # --- 4. An unpaired number may exist, but only in the one state that precedes
    #        pairing. This is what stops a NULL imei ever reaching a state the AE app
    #        can see, and it is the database-level counterpart of the read filters
    #        shipped in this same commit.
    op.execute(
        "ALTER TABLE fwa_inventory ADD CONSTRAINT imei_required_once_paired "
        "CHECK (status = 'MSISDN_ISSUED' OR imei IS NOT NULL) NOT VALID"
    )
    op.execute("ALTER TABLE fwa_inventory VALIDATE CONSTRAINT imei_required_once_paired")
    op.execute(
        "COMMENT ON CONSTRAINT imei_required_once_paired ON fwa_inventory IS "
        "'A number supplied by IOH has no IMEI until the Device Partner pairs one to "
        "it. Every later state requires one, so an unpaired unit can never reach a "
        "status the mobile app renders.'"
    )

    # --- 5. Indexes the supply-chain queries need. Plain CREATE INDEX takes a SHARE
    #        lock, which blocks writes for the duration; on a table this size that is
    #        milliseconds. If fwa_inventory ever grows large enough for that to matter,
    #        these become CREATE INDEX CONCURRENTLY run outside a migration — Alembic
    #        wraps migrations in a transaction, and CONCURRENTLY cannot run inside one.
    op.execute(
        "CREATE INDEX ix_inv_msisdn_po ON fwa_inventory (msisdn_po_id) "
        "WHERE msisdn_po_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_inv_device_po ON fwa_inventory (device_po_id) "
        "WHERE device_po_id IS NOT NULL"
    )
    # Backs GET /admin/stock: what this MPX holds, grouped by state.
    op.execute("CREATE INDEX ix_inv_mpx_status ON fwa_inventory (mpx_id, status)")
    # Backs the AUTO allocation rule: FIFO on receipt date, oldest first.
    op.execute(
        "CREATE INDEX ix_inv_allocatable ON fwa_inventory (mpx_id, received_at, msisdn) "
        "WHERE status = 'RECEIVED'"
    )

    op.execute(
        "COMMENT ON TABLE fwa_inventory IS "
        "'The record of one HiFi AIR unit for its whole life: an MSISDN IOH issued, an "
        "IMEI the Device Partner paired to it, the orders it moved on, and the AE it "
        "was finally allocated to. Single source of truth for where a unit is — PO "
        "tables carry quantities and money, never a second copy of unit state. "
        "Only statuses ALLOCATED, ACTIVATED, RETURNED and BLOCKED are visible to the "
        "mobile app; see AE_VISIBLE_STATUSES in app/services/inventory.py.'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_inv_allocatable")
    op.execute("DROP INDEX IF EXISTS ix_inv_mpx_status")
    op.execute("DROP INDEX IF EXISTS ix_inv_device_po")
    op.execute("DROP INDEX IF EXISTS ix_inv_msisdn_po")
    op.execute("ALTER TABLE fwa_inventory DROP CONSTRAINT IF EXISTS imei_required_once_paired")

    # Any row still carrying a supply-chain status could not satisfy the original CHECK,
    # and an unpaired row could not satisfy the original NOT NULL. Refuse rather than
    # delete: these are real units, and a downgrade must not silently destroy them.
    op.execute(
        """
        DO $$
        DECLARE stuck INTEGER;
        BEGIN
            SELECT count(*) INTO stuck FROM fwa_inventory
             WHERE imei IS NULL
                OR status IN ('MSISDN_ISSUED','PAIRED','ASSIGNED','SHIPPED','RECEIVED');
            IF stuck > 0 THEN
                RAISE EXCEPTION
                    'Cannot downgrade: % fwa_inventory row(s) are unpaired or in a '
                    'supply-chain state. Resolve or remove them first.', stuck;
            END IF;
        END $$
        """
    )

    op.execute("ALTER TABLE fwa_inventory DROP CONSTRAINT ck_inv_status")
    op.execute(
        "ALTER TABLE fwa_inventory ADD CONSTRAINT ck_inv_status CHECK (status IN "
        "('AVAILABLE','ALLOCATED','CONSUMED','ACTIVATED','RETURNED','BLOCKED'))"
    )
    op.execute("ALTER TABLE fwa_inventory ALTER COLUMN imei SET NOT NULL")
    op.execute(
        """
        ALTER TABLE fwa_inventory
            DROP COLUMN received_at,
            DROP COLUMN paired_by_admin_id,
            DROP COLUMN paired_at,
            DROP COLUMN call_plan_id,
            DROP COLUMN brand_code,
            DROP COLUMN mpx_id,
            DROP COLUMN device_po_id,
            DROP COLUMN msisdn_po_id
        """
    )
