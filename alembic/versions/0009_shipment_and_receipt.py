"""Supply chain step 8 — shipment, delivery milestones, and goods receipt.

The Device Partner packs and ships with an AWB; the MPX tracks progress and finally
confirms receipt. Three decisions from the business shape this migration, and all three
made it smaller than the spec drafted it.

**Receipt is one final, all-or-nothing confirmation.** Confirmed 2026-09-01: if MPX
opens the box and finds 98 of 100, the reshipping happens entirely outside this system,
and the PO can only be confirmed once the full list is physically present. So there is
no ``DITERIMA_SEBAGIAN``, no ``goods_receipt_line`` with per-unit conditions, and no
``is_partial`` flag. A short delivery leaves the order at ``PERIKSA`` and the endpoint
refuses; nothing is recorded until it can be recorded truthfully.

That also means ``goods_receipt`` needs no line table at all. Once every unit on the
order must be present to confirm, the received set is exactly the ``fwa_inventory`` rows
carrying that ``device_po_id`` — and duplicating unit state into a PO-side table is what
golden rule 6 forbids.

**The courier is free text, and milestones are entered by hand.** Confirmed 2026-09-01:
keep it simple first. So there is no ``courier`` reference table, no
``GET /admin/reference/couriers``, and ``POST /admin/shipments/{id}/milestones`` needs
no service credential — a courier webhook can be added later as a dependency on one
route, without touching this schema.

**One shipment per order.** ``uq_shipment_device_po`` holds precisely because reshipping
is out of scope: an order is dispatched once, and if something goes wrong the correction
happens off-system before the receipt is confirmed.

Revision ID: 0009_shipment_and_receipt
Revises: 0008_ae_brand_scope
Create Date: 2026-09-01

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_shipment_and_receipt"
down_revision: str | None = "0008_ae_brand_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE shipment (
            shipment_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            device_po_id UUID NOT NULL REFERENCES device_po(device_po_id) ON DELETE CASCADE,
            courier_name VARCHAR(60) NOT NULL,
            awb          VARCHAR(60) NOT NULL,
            shipped_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            estimated_delivery_date DATE,
            delivered_at TIMESTAMPTZ,
            created_by_admin_id UUID REFERENCES admin_user(admin_user_id),
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            -- One dispatch per order. Safe because reshipping is handled off-system.
            CONSTRAINT uq_shipment_device_po UNIQUE (device_po_id),
            CONSTRAINT ck_shipment_awb CHECK (length(btrim(awb)) > 0),
            CONSTRAINT ck_shipment_courier CHECK (length(btrim(courier_name)) > 0),
            CONSTRAINT ck_shipment_eta CHECK
                (estimated_delivery_date IS NULL OR estimated_delivery_date >= shipped_at::date)
        )
        """
    )
    op.execute(
        "COMMENT ON COLUMN shipment.courier_name IS "
        "'Free text, entered by the Device Partner. Deliberately not a foreign key to a "
        "courier table: the business asked to keep this manual for v1.'"
    )
    op.execute(
        "COMMENT ON COLUMN shipment.awb IS "
        "'Nomor Resi — the proof of dispatch named in the business process. Written "
        'into the device_po status history as "{courier} | {awb}", which is what the '
        "Riwayat panel renders.'"
    )

    op.execute(
        """
        CREATE TABLE shipment_milestone (
            milestone_id BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
            shipment_id  UUID NOT NULL REFERENCES shipment(shipment_id) ON DELETE CASCADE,
            milestone    VARCHAR(20) NOT NULL,
            occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            note         TEXT,
            created_by_admin_id UUID REFERENCES admin_user(admin_user_id),
            CONSTRAINT ck_milestone CHECK (milestone IN
                ('SHIPPED','IN_TRANSIT','OUT_FOR_DELIVERY','DELIVERED')),
            -- The tracker shows each step once; recording the same one twice would draw
            -- a progress bar that goes backwards.
            CONSTRAINT uq_milestone_per_shipment UNIQUE (shipment_id, milestone)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_milestone_shipment ON shipment_milestone (shipment_id, occurred_at)"
    )
    op.execute(
        "COMMENT ON TABLE shipment_milestone IS "
        "'Backs the four-step Delivery Progress tracker exactly. Entered by hand by the "
        "Device Partner for v1 — no courier integration.'"
    )

    op.execute(
        """
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
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE goods_receipt IS "
        "'One row per completed receipt, and only ever a complete one. Confirmed "
        "2026-09-01: MPX can confirm only when every unit on the order is physically "
        "present, and a short delivery is resolved outside this system. There is "
        "therefore no line table and no partial flag — which units were received is "
        "exactly the fwa_inventory rows carrying this order id.'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS goods_receipt")
    op.execute("DROP TABLE IF EXISTS shipment_milestone")
    op.execute("DROP TABLE IF EXISTS shipment")
