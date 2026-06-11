"""add shift_id to invoices

Revision ID: f6f7c2f44dd3
Revises: p4d1a8b6c5e7
Create Date: 2026-06-11 14:55:32.629904

NB: shift_id was already added by migration o3c9f6d8e2a4 (ERP-01 Phase 3).
On databases that ran the Phase 3 chain in order, this column already
exists. We make the migration idempotent so the chain still upgrades
cleanly whether or not the column is already present.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f6f7c2f44dd3'
down_revision = 'p4d1a8b6c5e7'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def _has_index(table, index_name):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return any(idx["name"] == index_name for idx in insp.get_indexes(table))


def upgrade():
    if _has_col("invoices", "shift_id"):
        return    # Phase 3 migration already added it.
    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("shift_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_invoices_shift_id", "cashier_shifts",
            ["shift_id"], ["id"],
        )
    if not _has_index("invoices", "ix_invoices_shift_id"):
        try:
            op.create_index(
                "ix_invoices_shift_id", "invoices", ["shift_id"],
                unique=False,
            )
        except Exception:
            pass


def downgrade():
    # No-op when shift_id was actually added by the Phase 3 migration —
    # only the Phase 3 downgrade should drop it.
    pass
