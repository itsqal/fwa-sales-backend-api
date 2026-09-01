"""Supply chain step 7 — device purchase orders (MPX -> DP).

An MPX orders devices from a Device Partner; the DP accepts, attaches paired bundles to
the order, and later ships it. This migration adds the order and its history. Shipment
and goods receipt are step 8.

**``unit_price_idr`` is a snapshot, not a join.** The price is copied onto the order at
creation and never read back from ``device_model.list_price_idr`` again. A price change
six months from now must not silently rewrite the value of a closed order, and three
companies read these numbers.

**``device_po_status_history`` is a rendered screen, not bookkeeping.** The *Riwayat*
panel of the Detail PO modal displays ``(tanggal, status, oleh, keterangan)`` straight
from it, and the shipment step writes ``J&T Express | JD0463672772`` into ``note``. It
is only trustworthy because every transition writes here in the same transaction as the
status change — golden rule 8.

**``DITERIMA_SEBAGIAN`` is absent by decision.** The spec modelled a partial-receipt
state; the business rule confirmed on 2026-09-01 is that MPX can only confirm receipt
when every unit is present. One unit short and the receipt cannot complete, so the PO
stays at ``PERIKSA``. Modelling a state that can never be reached would be worse than
not modelling it.

Revision ID: 0007_device_po
Revises: 0006_msisdn_po
Create Date: 2026-09-01

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_device_po"
down_revision: str | None = "0006_msisdn_po"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE device_po_seq START WITH 1")
    op.execute(
        """
        CREATE TABLE device_po (
            device_po_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            po_code           VARCHAR(80) NOT NULL,
            mpx_id            UUID NOT NULL REFERENCES mpx(mpx_id),
            device_partner_id UUID NOT NULL REFERENCES device_partner(device_partner_id),
            device_model_id   UUID NOT NULL REFERENCES device_model(device_model_id),
            brand_code        VARCHAR(10) NOT NULL REFERENCES brand(code),
            qty               INTEGER NOT NULL,
            unit_price_idr    BIGINT  NOT NULL,
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
            CONSTRAINT ck_device_po_status CHECK (status IN
                ('DIAJUKAN','DIPROSES','DIKIRIM','PERIKSA','DITERIMA','DITOLAK','DIBATALKAN')),
            CONSTRAINT ck_device_po_qty   CHECK (qty > 0),
            CONSTRAINT ck_device_po_price CHECK (unit_price_idr > 0),
            -- Money is an integer of whole rupiah and the arithmetic is enforced here,
            -- so a client cannot submit a total that disagrees with its own line.
            CONSTRAINT ck_device_po_total CHECK (total_idr = unit_price_idr * qty),
            CONSTRAINT ck_device_po_phone CHECK
                (pic_phone IS NULL OR pic_phone ~ '^(62|0)[0-9]{8,13}$'),
            CONSTRAINT ck_device_po_rejected CHECK
                (status <> 'DITOLAK' OR rejected_reason IS NOT NULL)
        )
        """
    )
    op.execute("CREATE INDEX ix_device_po_mpx ON device_po (mpx_id, submitted_at DESC)")
    op.execute("CREATE INDEX ix_device_po_dp ON device_po (device_partner_id, submitted_at DESC)")
    op.execute("CREATE INDEX ix_device_po_status ON device_po (status, submitted_at DESC)")
    op.execute(
        "COMMENT ON COLUMN device_po.po_code IS "
        "'PO-{CIRCLE}-{BRAND}-{MODEL}-{YYYYMMDD}-{SEQ}, e.g. "
        "PO-JAVA-3ID-RABIT CPE-R-28-20250809-407. Generated server-side from "
        "device_po_seq; never accepted from a client.'"
    )
    op.execute(
        "COMMENT ON COLUMN device_po.unit_price_idr IS "
        "'Snapshot of device_model.list_price_idr at creation, in whole rupiah. "
        "Deliberately not a join: a later price change must never rewrite the value of "
        "a closed order.'"
    )

    op.execute(
        """
        CREATE TABLE device_po_status_history (
            history_id   BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
            device_po_id UUID NOT NULL
                           REFERENCES device_po(device_po_id) ON DELETE CASCADE,
            old_status   VARCHAR(20),
            new_status   VARCHAR(20) NOT NULL,
            changed_by_admin_id UUID REFERENCES admin_user(admin_user_id),
            note         TEXT,
            changed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_device_po_hist ON device_po_status_history (device_po_id, changed_at)"
    )
    op.execute(
        "COMMENT ON TABLE device_po_status_history IS "
        "'Backs the Riwayat panel of the Detail PO modal, which renders "
        "(tanggal, status, oleh, keterangan) directly from these rows. The shipment "
        "step writes the courier and AWB into note. Append-only, and written in the "
        "same transaction as the status change it records.'"
    )

    # The FK deferred by migration 0005, now that its target exists.
    op.execute(
        "ALTER TABLE fwa_inventory ADD CONSTRAINT fk_inv_device_po "
        "FOREIGN KEY (device_po_id) REFERENCES device_po(device_po_id) NOT VALID"
    )
    op.execute("ALTER TABLE fwa_inventory VALIDATE CONSTRAINT fk_inv_device_po")


def downgrade() -> None:
    op.execute("ALTER TABLE fwa_inventory DROP CONSTRAINT IF EXISTS fk_inv_device_po")
    op.execute("DROP TABLE IF EXISTS device_po_status_history")
    op.execute("DROP TABLE IF EXISTS device_po")
    op.execute("DROP SEQUENCE IF EXISTS device_po_seq")
