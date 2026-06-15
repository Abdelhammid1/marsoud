"""MARSOUD-46 follow-up — leads.notes column

Adds:
  - leads.notes   Text  nullable  — generic optional notes (separate
                                     from meeting_notes / lost_reason)

Revision ID: s7e9f4d8b3a1
Revises: r6f3c1e8d2b9
Create Date: 2026-06-15
"""
from alembic import op
import sqlalchemy as sa

revision = 's7e9f4d8b3a1'
down_revision = 'r6f3c1e8d2b9'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("leads", "notes"):
        with op.batch_alter_table("leads", schema=None) as batch_op:
            batch_op.add_column(sa.Column("notes", sa.Text(), nullable=True))


def downgrade():
    if _has_col("leads", "notes"):
        with op.batch_alter_table("leads", schema=None) as batch_op:
            batch_op.drop_column("notes")
