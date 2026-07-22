"""MARSOUD-EMAIL-VERIFY (Abdelhamid 2026-07-22).

Adds `users.email_verified_at DATETIME NULL`. Existing rows stay NULL —
they were signed up before the verification requirement existed, so
we grandfather them in via a one-shot backfill: any user with
status='ACTIVE' before this migration gets email_verified_at =
users.created_at so the middleware never nags them.

The PENDING_VERIFICATION status value is app-level (enum), no schema
change needed for it.

Revision ID: c7d3e8f6a2b1
Revises: b6be6631d1cb
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'c7d3e8f6a2b1'
down_revision = 'b6be6631d1cb'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("users", "email_verified_at"):
        with op.batch_alter_table("users", schema=None) as batch:
            batch.add_column(
                sa.Column("email_verified_at", sa.DateTime(), nullable=True))
    # Backfill — existing ACTIVE users predate the requirement.
    op.execute(
        "UPDATE users SET email_verified_at = created_at "
        "WHERE email_verified_at IS NULL AND status = 'ACTIVE'"
    )


def downgrade():
    if _has_col("users", "email_verified_at"):
        with op.batch_alter_table("users", schema=None) as batch:
            batch.drop_column("email_verified_at")
