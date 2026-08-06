"""MARSOUD-ATTENDANCE-CHECKIN (2026-08-05) — the employee's own record.

Until now the only attendance data was the exception: HR typing in, after
the fact, that someone was absent or late. Nobody recorded the ordinary
case of turning up, so there was nothing to measure an exception against.

One row per employee per day, enforced by a unique constraint the same
way attendance_exceptions does it — check-out updates the row rather than
creating a second, so "arrived twice" is impossible by construction.

Coordinates are nullable: a refused browser permission must not block the
check-in. Location is evidence when offered, never a gate.

Revision ID: o5x8t1z4s9u0
Revises: n4w7s0y3r8t9
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'o5x8t1z4s9u0'
down_revision = 'n4w7s0y3r8t9'
branch_labels = None
depends_on = None

TABLE = "attendance_checkins"


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Integer,
                  sa.ForeignKey("companies.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("employee_id", sa.Integer,
                  sa.ForeignKey("employees.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("date", sa.Date, nullable=False, index=True),
        sa.Column("check_in_time", sa.DateTime),
        sa.Column("check_out_time", sa.DateTime),
        sa.Column("check_in_lat", sa.Float),
        sa.Column("check_in_lng", sa.Float),
        sa.Column("check_out_lat", sa.Float),
        sa.Column("check_out_lng", sa.Float),
        sa.Column("created_at", sa.DateTime),
        sa.UniqueConstraint("employee_id", "date",
                            name="uq_attendance_checkin_employee_date"),
    )


def downgrade():
    if _has_table(TABLE):
        op.drop_table(TABLE)
