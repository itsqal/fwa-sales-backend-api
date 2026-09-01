"""Supply chain step 9 — allocation to a salesman. The seam closes here.

This is the migration the whole programme has been building towards. Everything before
it moves stock between three companies with no effect on the mobile app; the first
successful allocation sets ``fwa_inventory.allocated_ae_id`` and a unit becomes visible
to an Account Executive's barcode scanner.

**``allocated_ae_id`` has exactly one writer.** ``POST /admin/allocations``, and only for
bundles that are ``RECEIVED`` and belong to the caller's own MPX. Same discipline as
``activation.activation_date``, which only the GA feed may set — the value gates what a
salesman can sell, so a second writer would be a way to hand someone else's stock away.

**AUTO allocation is FIFO on receipt date**, which the modal states in Indonesian:
*"Apabila tidak memilih MSISDN, alokasi akan mengutamakan modem yang masuk stok lebih
awal"*. ``ix_inv_allocatable`` from migration 0005 already indexes
``(mpx_id, received_at, msisdn) WHERE status = 'RECEIVED'`` for exactly this query. The
tiebreaker on ``msisdn`` is not decoration: without a total order, two concurrent
allocations of the last few units can interleave unpredictably.

**``uq_allocation_item_msisdn`` means a unit is allocated once, ever.** Transcribed from
the spec. Worth knowing what it costs: a unit an AE returns (status ``RETURNED``) can
never be re-allocated through this table without relaxing the constraint. There is no
return-to-stock flow in v1, so nothing needs it yet — but if one is ever built, this is
the constraint it will collide with, and the fix is to scope uniqueness to live
allocations rather than to drop it.

Revision ID: 0010_stock_allocation
Revises: 0009_shipment_and_receipt
Create Date: 2026-09-01

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_stock_allocation"
down_revision: str | None = "0009_shipment_and_receipt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE stock_allocation (
            stock_allocation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            mpx_id UUID NOT NULL REFERENCES mpx(mpx_id),
            ae_id  UUID NOT NULL REFERENCES account_executive(ae_id),
            allocated_by_admin_id UUID NOT NULL REFERENCES admin_user(admin_user_id),
            allocated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            mode   VARCHAR(10) NOT NULL,
            qty    INTEGER     NOT NULL,
            note   TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_allocation_mode CHECK (mode IN ('AUTO','MANUAL')),
            CONSTRAINT ck_allocation_qty  CHECK (qty > 0)
        )
        """
    )
    op.execute("CREATE INDEX ix_allocation_mpx ON stock_allocation (mpx_id, allocated_at DESC)")
    op.execute("CREATE INDEX ix_allocation_ae  ON stock_allocation (ae_id, allocated_at DESC)")
    op.execute(
        "COMMENT ON TABLE stock_allocation IS "
        "'One handover of stock from an MPX to an Account Executive. AUTO picks units "
        "FIFO by received_at; MANUAL names them. This is the table whose write makes "
        "stock visible to the mobile app.'"
    )

    op.execute(
        """
        CREATE TABLE stock_allocation_item (
            stock_allocation_id UUID NOT NULL
                REFERENCES stock_allocation(stock_allocation_id) ON DELETE CASCADE,
            msisdn VARCHAR(15) NOT NULL REFERENCES fwa_inventory(msisdn),
            PRIMARY KEY (stock_allocation_id, msisdn),
            -- A unit is allocated once. See the module docstring on what this costs if a
            -- return-to-stock flow is ever built.
            CONSTRAINT uq_allocation_item_msisdn UNIQUE (msisdn)
        )
        """
    )
    op.execute(
        "COMMENT ON CONSTRAINT uq_allocation_item_msisdn ON stock_allocation_item IS "
        "'A physical unit cannot be in two salesmen''s hands. The row lock in the "
        "allocation service is what makes concurrent allocation fail cleanly; this is "
        "the backstop that makes it impossible.'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS stock_allocation_item")
    op.execute("DROP TABLE IF EXISTS stock_allocation")
