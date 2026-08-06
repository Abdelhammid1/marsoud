"""MARSOUD-ATTENDANCE-AUTO — the absence sweep needs an explicit switch.

Found by auditing stage B, not by using it.

Lateness is opt-in by behaviour: it can only fire for an employee who
actually checked in, so writing down the working hours costs nothing.
ABSENCE is the opposite — it fires for everyone who did NOT check in,
which on day one is the entire company.

Measured: three employees, one fresh policy, zero check-ins produced
three ABSENT exceptions on the very first sweep — a full day's pay each,
and again every working day after. The realistic rollout is "write the
hours down, then tell staff to start checking in", and the cron sweep
runs in the gap between those two steps.

Defaults to FALSE, including for the policies that already exist, so
turning attendance enforcement on is always a deliberate act.

Revision ID: p6y9u2a5t0v1
Revises: o5x8t1z4s9u0
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'p6y9u2a5t0v1'
down_revision = 'o5x8t1z4s9u0'
branch_labels = None
depends_on = None

TABLE = "attendance_policies"
COL = "auto_absent_enabled"


def _cols():
    insp = sa.inspect(op.get_bind())
    if TABLE not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(TABLE)}


def upgrade():
    if TABLE not in sa.inspect(op.get_bind()).get_table_names():
        return
    if COL not in _cols():
        op.add_column(TABLE, sa.Column(COL, sa.Boolean, nullable=False,
                                       server_default=sa.false()))


def downgrade():
    if COL in _cols():
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COL)
