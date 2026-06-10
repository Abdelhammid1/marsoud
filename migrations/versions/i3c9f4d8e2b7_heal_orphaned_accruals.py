"""One-time heal: clear settled_at on accruals whose settlement journal was reversed

Revision ID: i3c9f4d8e2b7
Revises: h2f5e8c91a44
Create Date: 2026-06-10 14:00:00

Context: prior to commit e47adbb, reverse_journal() flipped a settlement
journal entry's debits/credits in the ledger but never reset the source
EmployeeAccrual.settled_at. The fix in e47adbb wires reverse_journal to
clear settled_at + settlement_journal_entry_id on the matching accrual.
But it only fixes FUTURE reverses; any reverse-of-a-settlement that
happened pre-fix leaves an EmployeeAccrual row with settled_at still
timestamped even though the ledger has long since flipped — abdelhamid's
exact scenario (MARSOUD-28).

This migration walks those dirty rows once and heals them:
  - Find every EmployeeAccrual where settled_at IS NOT NULL AND
    settlement_journal_entry_id points at a JournalEntry that was reversed.
  - Clear settled_at and settlement_journal_entry_id on each.

Safe to run blindly — the condition (settlement journal is verifiably
reversed) is restrictive enough that it only touches genuinely-dirty
rows. Re-running does nothing because the fixed rows no longer match.

Schema-wise this is a no-op (DDL); it's purely a data fix expressed in
an Alembic upgrade for free chaining + idempotent re-run.
"""
from alembic import op
import sqlalchemy as sa


revision = "i3c9f4d8e2b7"
down_revision = "h2f5e8c91a44"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    if "employee_accruals" not in tables or "journal_entries" not in tables:
        # Nothing to heal on fresh installs that don't have payroll set up yet.
        return

    # Find accruals where settlement_journal_entry was reversed.
    # A reversal is a JournalEntry with reversal_of = settlement_journal_entry_id.
    result = conn.execute(sa.text("""
        SELECT a.id
        FROM employee_accruals a
        WHERE a.settled_at IS NOT NULL
          AND a.settlement_journal_entry_id IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM journal_entries je
            WHERE je.reversal_of = a.settlement_journal_entry_id
          )
    """))
    dirty_ids = [row[0] for row in result]
    if not dirty_ids:
        return

    # Clear the orphaned settle state. Two updates so we don't lose the
    # link in case anyone needs the journal id for forensics — but the
    # spec calls for resetting both, so we do.
    conn.execute(
        sa.text(
            "UPDATE employee_accruals "
            "SET settled_at = NULL, settlement_journal_entry_id = NULL "
            f"WHERE id IN ({','.join(str(i) for i in dirty_ids)})"
        )
    )
    # Print to alembic's log so the operator sees the count after deploy.
    print(f"  → healed {len(dirty_ids)} dirty accrual row(s) "
          f"(settled_at cleared on pre-fix-reversed settlements)")


def downgrade():
    # This is a one-way data fix. There's no safe way to re-dirty the
    # rows because we'd need to know which were genuinely dirty vs which
    # were always clean.
    pass
