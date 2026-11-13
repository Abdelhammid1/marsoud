"""Task schedules: run_time + excluded weekdays/dates  # MARSOUD-TASK-P73

Revision ID: e9d4a6b2c8f1
Revises: c5a7e2f9b1d3
Create Date: 2026-11-14 02:40:00
"""
from alembic import op
import sqlalchemy as sa


revision = "e9d4a6b2c8f1"
down_revision = "c5a7e2f9b1d3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("task_schedules") as b:
        b.add_column(sa.Column("run_time", sa.String(5), nullable=True))
        b.add_column(sa.Column("exclude_weekdays", sa.String(20), nullable=True))
        b.add_column(sa.Column("exclude_dates", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("task_schedules") as b:
        b.drop_column("exclude_dates")
        b.drop_column("exclude_weekdays")
        b.drop_column("run_time")
