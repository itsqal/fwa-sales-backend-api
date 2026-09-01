"""Confirmed reference data: device catalogue prices, and the Palembang stock point.

All values here were confirmed by the business on 2026-09-01. They are held as data
rather than as constants in code, and a future change is an UPDATE — never an edit to
this file, which has already run.

**Device prices.** Until now every model had ``list_price_idr = NULL``, which by design
made it impossible to raise a device PO: a guessed price on a purchase order that three
companies sign off is worse than a blocked form. These are the real figures.

**The catalogue rows are also inserted here, guarded.** Every other reference table in
the supply chain — ``device_partner``, ``mpx``, ``call_plan``, ``brand`` — is seeded by
its migration, but ``device_model`` has historically been populated by a hand-written
INSERT in the deployment guide. That left a fresh production database with no catalogue
and therefore no orderable devices. The insert below is keyed on ``model_code`` and
skips anything already present, so it is a no-op on any environment that already has
these rows.

⚠️ Before running this on an environment you did not build from these migrations,
check what is already in ``device_model``::

    SELECT model_code, brand, list_price_idr FROM device_model ORDER BY model_code;

If that environment records the same devices under *different* codes, stop: inserting
these would split inventory across two rows for one physical device, which is exactly
the failure UI Review issue #8 was about. Reconcile the codes first.

**MPX Palembang.** ``AE-TESTDEPLOY1`` already points at ``MPX-PLB-01`` through
``account_executive.mpx_code``, but no such stock point existed — so that salesman was
invisible to every MPX admin and could not be allocated stock. This closes the gap.

Revision ID: 0011_seed_prices_and_mpx
Revises: 0010_stock_allocation
Create Date: 2026-09-01

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_seed_prices_and_mpx"
down_revision: str | None = "0010_stock_allocation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Confirmed 2026-09-01. Whole rupiah, per golden rule 10.
PRICES = {
    "ADVAN V1 PRO": 390_000,
    "RABIT CPE-XR": 300_000,
    "HKM 127+": 350_000,
}


def upgrade() -> None:
    # The catalogue itself, guarded on model_code so this cannot duplicate a row.
    # network_generation is left alone: categorising a model here would be inventing
    # master data, and an uncategorised model accrues no incentive by design.
    op.execute(
        """
        INSERT INTO device_model (model_code, brand, sku)
        SELECT v.model_code, v.brand, v.sku
        FROM (VALUES
            ('HKM 127+',     'HKM',   'SKU-HKM-127P'),
            ('RABIT CPE-XR', 'RABIT', 'SKU-RBT-XR'),
            ('ADVAN V1 PRO', 'ADVAN', 'SKU-ADV-V1P')
        ) AS v(model_code, brand, sku)
        WHERE NOT EXISTS (
            SELECT 1 FROM device_model d WHERE d.model_code = v.model_code
        )
        """
    )

    # Link each model to the Device Partner that supplies it, for any row the earlier
    # backfill did not reach (a freshly inserted one, above).
    op.execute(
        """
        UPDATE device_model dm
           SET device_partner_id = dp.device_partner_id
          FROM device_partner dp
         WHERE upper(dm.brand) = dp.code
           AND dm.device_partner_id IS NULL
        """
    )

    values = ", ".join(f"('{code}', {price})" for code, price in PRICES.items())
    op.execute(
        f"""
        UPDATE device_model dm
           SET list_price_idr = v.price
          FROM (VALUES {values}) AS v(model_code, price)
         WHERE dm.model_code = v.model_code
        """
    )

    # MPX-PLB-01 — referenced by an existing AE but never seeded.
    op.execute(
        """
        INSERT INTO mpx (code, name, legal_name, circle, region_id)
        SELECT 'MPX-PLB-01', 'MPX Palembang', 'PT Internet Rakyat Makmur', 'SUMATERA',
               (SELECT region_id FROM region WHERE region_code = 'PALEMBANG')
        WHERE NOT EXISTS (SELECT 1 FROM mpx WHERE code = 'MPX-PLB-01')
        """
    )


def downgrade() -> None:
    # Prices are cleared; the catalogue rows are NOT deleted. A device model may already
    # be referenced by fwa_inventory and by closed purchase orders, and a schema
    # rollback must not destroy the record of physical stock.
    values = ", ".join(f"'{code}'" for code in PRICES)
    op.execute(f"UPDATE device_model SET list_price_idr = NULL WHERE model_code IN ({values})")
    op.execute(
        """
        DELETE FROM mpx
         WHERE code = 'MPX-PLB-01'
           AND NOT EXISTS (SELECT 1 FROM admin_user  a WHERE a.mpx_id = mpx.mpx_id)
           AND NOT EXISTS (SELECT 1 FROM device_po   d WHERE d.mpx_id = mpx.mpx_id)
           AND NOT EXISTS (SELECT 1 FROM fwa_inventory i WHERE i.mpx_id = mpx.mpx_id)
        """
    )
