"""MARSOUD-PLAN-SSOT (2026-08-17) — belt-and-suspenders backfill for
the legacy `companies.plan` String column, which used to default to
`"FREE"` and was the primary source of the "FREE" label leaking into
the super-admin UI.

We do NOT drop the column here — too many code paths still reference
it (safest path is to leave the schema and simply not display it).
What we do is: wipe every `"FREE"` value to an empty string so any
future accidental read renders empty instead of a fake plan name.

`plan_snapshot(company)` (app/services/plan_snapshot.py) is the
single source of truth going forward. Every UI render + API response
+ dashboard banner goes through it.

Idempotent. Safe to run on a DB where `companies.plan` was already
cleaned, or where the column doesn't exist.

Revision ID: k8l1m4n7p0q3
Revises: j7k0l3m6n9p2
Create Date: 2026-08-17
"""
from alembic import op
import sqlalchemy as sa


revision = "k8l1m4n7p0q3"
down_revision = "j7k0l3m6n9p2"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table, col):
    try:
        insp = _inspector()
        if table not in insp.get_table_names():
            return False
        return any(c["name"] == col for c in insp.get_columns(table))
    except Exception:
        return False


def upgrade():
    # Nothing to do if the column isn't there anymore (someone dropped
    # it in a future ticket) — this migration is deliberately a no-op
    # in that case.
    if not _has_column("companies", "plan"):
        return
    bind = op.get_bind()
    # UPDATE …WHERE plan = 'FREE' — case-sensitive on the exact legacy
    # default. Leaves any manually-set values ('PRO', 'ENTERPRISE',
    # tenant-custom strings) alone.
    bind.execute(
        sa.text(
            "UPDATE companies SET plan = '' WHERE plan = 'FREE'"
        )
    )


def downgrade():
    # Restore the "FREE" default for the rows we blanked. Best-effort —
    # any subsequent writes after the upgrade would drift, but we
    # don't have a way to recover the exact previous state anyway.
    if not _has_column("companies", "plan"):
        return
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE companies SET plan = 'FREE' "
            "WHERE plan = '' OR plan IS NULL"
        )
    )
