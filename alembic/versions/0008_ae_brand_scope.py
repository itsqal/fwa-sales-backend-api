"""AE brand scope — which telco brands a salesman may sell.

Resolves UI Review issue #9. The MPX *Tentang AE* panel shows ``Brand: Hybrid`` beside
the AE code and branch, and ``Hybrid`` is not one of the two brands — it means the AE
may sell either. Confirmed 2026-09-01.

**Deliberately not a row in ``brand``.** ``HYBRID`` is a capability of a person, not a
telco brand. Adding it to the ``brand`` table would let it leak into
``msisdn_po.brand_code``, ``device_po.brand_code`` and ``fwa_inventory.brand_code``,
where a "hybrid" unit is meaningless — a physical SIM is one brand or the other.

**Nullable, and read only by the dashboard.** The column is added to a table the
deployed mobile app authenticates against, so the change is kept as small as it can be:
adding a nullable column is metadata-only on PostgreSQL 11+, nothing backfills, and the
field is exposed only on the new ``/admin/account-executives`` endpoints. The
``AccountExecutive`` schema the mobile app receives is byte-identical after this
migration. If the app ever needs the field, that is an additive contract change on its
own schedule.

NULL means "not recorded", not "none". Every AE that exists today is NULL, and nothing
in v1 branches on the value — it is rendered, not enforced. Should it later gate
allocation, NULL has to keep meaning "unrestricted", or the rule would block every AE
already in the field on the day it ships.

Revision ID: 0008_ae_brand_scope
Revises: 0007_device_po
Create Date: 2026-09-01

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_ae_brand_scope"
down_revision: str | None = "0007_device_po"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE account_executive
            ADD COLUMN brand_scope VARCHAR(10),
            ADD CONSTRAINT ck_ae_brand_scope CHECK
                (brand_scope IS NULL OR brand_scope IN ('IM3','3ID','HYBRID'))
        """
    )
    op.execute(
        "COMMENT ON COLUMN account_executive.brand_scope IS "
        "'Which telco brands this AE may sell: IM3, 3ID, or HYBRID for either. A "
        "capability of the person, NOT a brand — deliberately not a row in the brand "
        "table, because a physical SIM is one brand or the other and a HYBRID unit "
        "would be meaningless. NULL means not recorded; nothing branches on it in v1, "
        "and if it ever gates allocation, NULL must keep meaning unrestricted.'"
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE account_executive
            DROP CONSTRAINT ck_ae_brand_scope,
            DROP COLUMN brand_scope
        """
    )
