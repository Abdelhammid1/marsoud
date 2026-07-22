"""MARSOUD-LOCKOUT-RESET (Abdelhamid 2026-07-22).

Adds two columns to `users` for the account-lockout side of the ticket:

  · failed_login_attempts INT DEFAULT 0  — bumped on wrong-password,
    reset to 0 on a successful login. When it hits 5 the login route
    also sets `locked_until` and refuses further attempts until the
    lock window passes.

  · locked_until DATETIME NULL  — when set + future, refuses login
    with a friendly Arabic message showing remaining time.

The forgot-password side of the ticket needs no schema change — the
reset token is a signed URLSafeTimedSerializer payload with the
"marsoud-password-reset" salt, verified against user_id + password_hash
so a token is invalidated the moment the password changes.

Revision ID: d8e1f4a9c3b2
Revises: c7d3e8f6a2b1
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'd8e1f4a9c3b2'
down_revision = 'c7d3e8f6a2b1'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch:
        if not _has_col("users", "failed_login_attempts"):
            batch.add_column(sa.Column(
                "failed_login_attempts", sa.Integer(),
                nullable=False, server_default="0"))
        if not _has_col("users", "locked_until"):
            batch.add_column(sa.Column(
                "locked_until", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("users", schema=None) as batch:
        if _has_col("users", "locked_until"):
            batch.drop_column("locked_until")
        if _has_col("users", "failed_login_attempts"):
            batch.drop_column("failed_login_attempts")
