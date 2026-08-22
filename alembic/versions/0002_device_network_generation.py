"""Device network generation and the agreed AE activation incentive tiers.

Hand-written for the same reason as 0001: autogenerate does not see CHECK constraints,
and it would never emit the ``incentive_rule`` rows at all. The rules are business data,
not code — a future revision of the amounts is a new pair of rows with a later
``effective_from``, which leaves every already-paid ledger entry pointing at the rule
that actually paid it.

``network_generation`` is left NULL for existing catalogue rows. Categorising HKM 127+
or ADVAN V1 PRO here would be inventing master data (CLAUDE.md §12); an uncategorised
model accrues nothing and logs a warning instead, because an underpayment can be fixed
by a data correction and a replayed feed, while an overpayment is a payroll incident.

Revision ID: 0002_device_network_generation
Revises: 0001_initial_ae_schema
Create Date: 2026-08-22

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_device_network_generation"
down_revision: str | None = "0001_initial_ae_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The date the tiers were agreed with the business. Deliberately not retroactive: the
# accrual engine runs at GA-ingest time, so this reads as "every Gross Add from here on".
EFFECTIVE_FROM = "2026-08-22"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE device_model
            ADD COLUMN network_generation VARCHAR(10),
            ADD CONSTRAINT ck_device_model_netgen CHECK
                (network_generation IS NULL OR network_generation IN ('4G','5G'))
        """
    )
    op.execute(
        "COMMENT ON COLUMN device_model.network_generation IS "
        "'Radio generation of the CPE. Selects the incentive tier on a confirmed Gross "
        "Add: a 4G unit accrues Rp 35.000, a 5G unit Rp 135.000. Deliberately nullable "
        "— an uncategorised model accrues nothing and logs a warning, because "
        "underpaying is recoverable and overpaying is a payroll incident.'"
    )
    op.execute(
        "COMMENT ON COLUMN incentive_rule.conditions IS "
        "'Predicate the accrual engine must satisfy before writing a ledger entry. "
        'Exactly one key is implemented: "networkGeneration" ("4G" | "5G"), '
        "matched against the device model behind the activated MSISDN. An empty object "
        "always matches. A rule carrying any other key is skipped and logged — an "
        "unevaluable rule must never pay out.'"
    )

    # Guarded rather than a plain VALUES insert. `downgrade()` deliberately keeps rules
    # that have already paid out, so a downgrade followed by a re-upgrade would
    # otherwise insert a second copy of each tier — and every future Gross Add would
    # accrue twice.
    op.execute(
        f"""
        INSERT INTO incentive_rule
            (rule_name, event_type, amount_idr, conditions, effective_from)
        SELECT v.rule_name, 'ACTIVATION', v.amount_idr, v.conditions::jsonb,
               DATE '{EFFECTIVE_FROM}'
        FROM (VALUES
            ('AE GA activation - 4G modem',  35000.00, '{{"networkGeneration": "4G"}}'),
            ('AE GA activation - 5G modem', 135000.00, '{{"networkGeneration": "5G"}}')
        ) AS v(rule_name, amount_idr, conditions)
        WHERE NOT EXISTS (
            SELECT 1 FROM incentive_rule r
            WHERE r.rule_name = v.rule_name
              AND r.effective_from = DATE '{EFFECTIVE_FROM}'
        )
        """
    )


def downgrade() -> None:
    # Only the rules this migration inserted, and only while nothing has accrued
    # against them — a ledger entry is a record of money owed and must outlive a
    # schema rollback.
    op.execute(
        f"""
        DELETE FROM incentive_rule r
        WHERE r.event_type = 'ACTIVATION'
          AND r.effective_from = DATE '{EFFECTIVE_FROM}'
          AND r.conditions ? 'networkGeneration'
          AND NOT EXISTS (SELECT 1 FROM incentive_ledger l WHERE l.rule_id = r.rule_id)
        """
    )
    op.execute(
        """
        ALTER TABLE device_model
            DROP CONSTRAINT ck_device_model_netgen,
            DROP COLUMN network_generation
        """
    )
