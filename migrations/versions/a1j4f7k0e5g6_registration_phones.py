"""MARSOUD-REGISTRATION-PHONES-01 (Batch 6 Ticket 4, 2026-07-29).

Two nullable phone columns:
  · companies.phone — the company's official contact number
    (billing/legal). Surfaces on invoices/PDFs + super-admin.
  · users.phone — the user's personal contact number. Used by
    Manasty support when email bounces.

Both nullable + idempotent so existing rows keep working with
NULL until owners fill them in.

Revision ID: a1j4f7k0e5g6
Revises: z0i3e6h9d4f5
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1j4f7k0e5g6'
down_revision = 'z0i3e6h9d4f5'
branch_labels = None
depends_on = None


def _has_col(insp, table, col):
    return any(c["name"] == col for c in insp.get_columns(table))


def upgrade():
    insp = sa.inspect(op.get_bind())
    if not _has_col(insp, "companies", "phone"):
        with op.batch_alter_table("companies") as batch:
            batch.add_column(sa.Column(
                "phone", sa.String(50), nullable=True))
    if not _has_col(insp, "users", "phone"):
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column(
                "phone", sa.String(50), nullable=True))


def downgrade():
    insp = sa.inspect(op.get_bind())
    if _has_col(insp, "users", "phone"):
        with op.batch_alter_table("users") as batch:
            batch.drop_column("phone")
    if _has_col(insp, "companies", "phone"):
        with op.batch_alter_table("companies") as batch:
            batch.drop_column("phone")
