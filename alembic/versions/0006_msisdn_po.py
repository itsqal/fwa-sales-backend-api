"""Supply chain step 5 — MSISDN procurement, and shared write infrastructure.

The first complete use case: a Device Partner asks IOH for numbers, IOH supplies them,
and the supplied numbers become ``fwa_inventory`` rows. It is also where three pieces of
machinery that steps 6 and 7 reuse first appear.

**There is no ``msisdn_po_item`` table.** A supplied MSISDN *is* an inventory unit, so
IOH's supply step inserts straight into ``fwa_inventory`` with ``msisdn_po_id`` set and
``imei`` still NULL. A separate item table would mean two rows per number that must
agree with each other forever. Same reasoning that collapsed ``pjp`` into ``customer``
in the AE model.

**``idempotency_record`` is new, and not in the spec.** Golden rule 10 requires a
400-row import to be replay-safe, but the existing AE pattern hangs the key on the
created row's own table (``uq_cust_idem`` and friends). That does not work here: the
supply endpoint inserts N ``fwa_inventory`` rows rather than one aggregate, and the
pairing endpoint in step 6 creates no rows at all — it updates existing ones. So the key
needs somewhere of its own, along with the response it produced, so a retry after a
dropped connection returns the original answer instead of inserting 400 more numbers.

**PO codes come from a sequence, never from the client.** The format read off the IOH
dashboard is ``{DP_CODE}-{YYYYMMDD}-{SEQ}`` -> ``ADVAN-20250523-207``. A sequence makes
the number atomic and collision-free under concurrency; ``uq_msisdn_po_code`` is the
backstop if anyone ever generates one another way.

Revision ID: 0006_msisdn_po
Revises: 0005_fwa_inventory_lifecycle
Create Date: 2026-09-01

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_msisdn_po"
down_revision: str | None = "0005_fwa_inventory_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -----------------------------------------------------------------
    # Shared: replay safety for bulk writes.
    # -----------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE idempotency_record (
            idempotency_record_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            admin_user_id UUID NOT NULL
                            REFERENCES admin_user(admin_user_id) ON DELETE CASCADE,
            endpoint      VARCHAR(120) NOT NULL,
            key           UUID         NOT NULL,
            request_hash  TEXT         NOT NULL,
            response_body JSONB        NOT NULL,
            status_code   SMALLINT     NOT NULL,
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_idem_admin_endpoint_key UNIQUE (admin_user_id, endpoint, key)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE idempotency_record IS "
        "'Replay cache for admin writes that do not create a single owning row — bulk "
        "MSISDN supply, IMEI pairing, bundle attachment. Scoped per admin and endpoint "
        "so one caller cannot replay another caller''s write. request_hash detects the "
        "same key reused with a different payload, which is a client bug and returns "
        "409 rather than silently answering the wrong question.'"
    )
    op.execute(
        "COMMENT ON COLUMN idempotency_record.response_body IS "
        "'The original response, returned verbatim on replay. Storing the answer rather "
        "than recomputing it is the point: a retry after a dropped connection must not "
        "re-run a 400-row insert to discover it already happened.'"
    )

    # -----------------------------------------------------------------
    # SUPPLY CHAIN — MODULE G — MSISDN procurement (DP -> IOH)
    # -----------------------------------------------------------------
    op.execute("CREATE SEQUENCE msisdn_po_seq START WITH 1")
    op.execute(
        """
        CREATE TABLE msisdn_po (
            msisdn_po_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            po_code           VARCHAR(40)  NOT NULL,
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
            -- IOH has discretion to refuse (confirmed 2026-09-01), and a refusal the DP
            -- cannot act on is not a refusal. The reason is required by the database,
            -- not merely by the request schema.
            CONSTRAINT ck_msisdn_po_rejected CHECK
                (status <> 'DITOLAK' OR rejected_reason IS NOT NULL)
        )
        """
    )
    op.execute("CREATE INDEX ix_msisdn_po_dp ON msisdn_po (device_partner_id, submitted_at DESC)")
    op.execute("CREATE INDEX ix_msisdn_po_status ON msisdn_po (status, submitted_at DESC)")
    op.execute(
        "COMMENT ON COLUMN msisdn_po.po_code IS "
        "'{DP_CODE}-{YYYYMMDD}-{SEQ}, e.g. ADVAN-20250523-207. Generated server-side "
        "from msisdn_po_seq; never accepted from a client.'"
    )
    op.execute(
        "COMMENT ON TABLE msisdn_po IS "
        "'A Device Partner''s request for MSISDNs. There is deliberately no item table: "
        "a supplied number IS an fwa_inventory row, carrying this msisdn_po_id.'"
    )

    op.execute(
        """
        CREATE TABLE msisdn_po_status_history (
            history_id   BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
            msisdn_po_id UUID NOT NULL
                           REFERENCES msisdn_po(msisdn_po_id) ON DELETE CASCADE,
            old_status   VARCHAR(20),
            new_status   VARCHAR(20) NOT NULL,
            changed_by_admin_id UUID REFERENCES admin_user(admin_user_id),
            note         TEXT,
            changed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_msisdn_po_hist ON msisdn_po_status_history (msisdn_po_id, changed_at)"
    )
    op.execute(
        "COMMENT ON TABLE msisdn_po_status_history IS "
        "'Append-only. Written in the same transaction as the status change it records, "
        "which is the only reason the Riwayat panel can be trusted.'"
    )

    # -----------------------------------------------------------------
    # The FK deferred by migration 0005, now that its target exists.
    # -----------------------------------------------------------------
    op.execute(
        "ALTER TABLE fwa_inventory ADD CONSTRAINT fk_inv_msisdn_po "
        "FOREIGN KEY (msisdn_po_id) REFERENCES msisdn_po(msisdn_po_id) NOT VALID"
    )
    op.execute("ALTER TABLE fwa_inventory VALIDATE CONSTRAINT fk_inv_msisdn_po")


def downgrade() -> None:
    op.execute("ALTER TABLE fwa_inventory DROP CONSTRAINT IF EXISTS fk_inv_msisdn_po")
    op.execute("DROP TABLE IF EXISTS msisdn_po_status_history")
    op.execute("DROP TABLE IF EXISTS msisdn_po")
    op.execute("DROP SEQUENCE IF EXISTS msisdn_po_seq")
    op.execute("DROP TABLE IF EXISTS idempotency_record")
