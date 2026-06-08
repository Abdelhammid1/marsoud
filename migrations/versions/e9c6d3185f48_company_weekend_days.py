"""Per-company weekend days (Cycle 6 audit gap close)

Revision ID: e9c6d3185f48
Revises: d8b4e2f17a31
Create Date: 2026-06-08 14:00:00

Adds:
  - companies.weekend_days  (String — CSV of Python weekday indices: "4,5" = Fri,Sat)

Empty / NULL means "use the default" (Friday + Saturday). Idempotent.
"""
from alembic import op
import sqlalchemy as sa


revision = "e9c6d3185f48"
down_revision = "d8b4e2f17a31"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table, column):
    return any(c["name"] == column for c in _inspector().get_columns(table))


def upgrade():
    with op.batch_alter_table("companies") as batch:
        if not _has_column("companies", "weekend_days"):
            batch.add_column(sa.Column("weekend_days", sa.String(20)))


def downgrade():
    with op.batch_alter_table("companies") as batch:
        if _has_column("companies", "weekend_days"):
            try:
                batch.drop_column("weekend_days")
            except Exception:
                pass
