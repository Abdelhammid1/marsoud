"""MARSOUD-ATTENDANCE-POLICY (2026-08-05) — what the working day is.

The HR module could record that someone was absent or late, but nothing
said what time they were supposed to arrive. Every exception was typed in
retroactively against a rule that lived in somebody's head.

Additive and inert: one new table, nothing existing is touched, and no
code path consults it yet. A company with no row here resolves to None
and keeps behaving exactly as it does today — which is what makes the
rest of this batch safe to deploy to existing tenants.

Revision ID: n4w7s0y3r8t9
Revises: m3v6r9x2q7s8
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'n4w7s0y3r8t9'
down_revision = 'm3v6r9x2q7s8'
branch_labels = None
depends_on = None

TABLE = "attendance_policies"

SCOPE = sa.Enum("COMPANY", "DEPARTMENT", "EMPLOYEE", name="policyscope")
PTYPE = sa.Enum("FIXED", "FLEXIBLE", name="policytype")


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
        sa.Column("scope", SCOPE, nullable=False,
                  server_default="COMPANY", index=True),
        sa.Column("department_id", sa.Integer,
                  sa.ForeignKey("departments.id", ondelete="CASCADE"),
                  nullable=True, index=True),
        sa.Column("employee_id", sa.Integer,
                  sa.ForeignKey("employees.id", ondelete="CASCADE"),
                  nullable=True, index=True),
        sa.Column("policy_type", PTYPE, nullable=False,
                  server_default="FIXED"),
        # FIXED
        sa.Column("start_time", sa.Time),
        sa.Column("end_time", sa.Time),
        sa.Column("work_days", sa.String(20)),
        # FLEXIBLE
        sa.Column("earliest_checkin", sa.Time),
        sa.Column("latest_checkin", sa.Time),
        sa.Column("required_hours_per_day", sa.Numeric(4, 2)),

        sa.Column("is_active", sa.Boolean, nullable=False,
                  server_default=sa.true(), index=True),
        sa.Column("created_at", sa.DateTime),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
    )


def downgrade():
    if _has_table(TABLE):
        op.drop_table(TABLE)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        SCOPE.drop(bind, checkfirst=True)
        PTYPE.drop(bind, checkfirst=True)
