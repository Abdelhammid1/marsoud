"""CRM activity time — Asmaa (2026-07-02) — allow meeting time.

Changes lead_activities.follow_up_date from Date to DateTime so an
activity can carry the actual meeting time (e.g. "Tuesday 15:00"),
not just the day. Existing Date rows survive the switch — SQLite
stores them as strings and Python reads "2026-07-15" as
"2026-07-15 00:00:00".

Revision ID: d6_e3f9a2b7c8d
Revises: d5_a9b2c8f4e3d
"""
from alembic import op
import sqlalchemy as sa


revision = "d6_e3f9a2b7c8d"
down_revision = "d5_a9b2c8f4e3d"
branch_labels = None
depends_on = None


def _col_type(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return None
    for c in insp.get_columns(table):
        if c["name"] == col:
            return str(c["type"]).upper()
    return None


def upgrade():
    current = _col_type("lead_activities", "follow_up_date")
    if current and "DATETIME" in current:
        return   # already migrated
    if current is None:
        return   # table missing (fresh DB before this ticket) — skip
    with op.batch_alter_table("lead_activities") as batch:
        batch.alter_column(
            "follow_up_date",
            existing_type=sa.Date(),
            type_=sa.DateTime(),
            existing_nullable=True,
        )


def downgrade():
    current = _col_type("lead_activities", "follow_up_date")
    if not current or "DATE" == current.strip():
        return
    with op.batch_alter_table("lead_activities") as batch:
        batch.alter_column(
            "follow_up_date",
            existing_type=sa.DateTime(),
            type_=sa.Date(),
            existing_nullable=True,
        )
