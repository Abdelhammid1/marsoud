"""super admin gaps — status/plan + errors table (Cycle 4 audit fix)

Revision ID: b5f2e3a91143
Revises: a4e1d2c80042
Create Date: 2026-06-05 13:00:00

Closes audit gaps reported after Cycle 4 build:
  - companies.status    (ACTIVE / SUSPENDED / TRIAL — supersedes is_active)
  - companies.plan      (FREE / PRO / ENTERPRISE — free text, default FREE)
  - platform_errors     (per-company error log, for /admin support tools)

Backfill: existing rows get status from is_active (False → SUSPENDED, else ACTIVE)
and plan="FREE". Idempotent.
"""
from alembic import op
import sqlalchemy as sa


revision = "b5f2e3a91143"
down_revision = "a4e1d2c80042"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_column(table, col):
    if not _has_table(table):
        return False
    return col in {c["name"] for c in _inspector().get_columns(table)}


def upgrade():
    # ── 1. companies.status + plan ─────────────────────────────────────
    if not _has_column("companies", "status"):
        with op.batch_alter_table("companies") as batch:
            batch.add_column(sa.Column("status", sa.String(20), nullable=False,
                                       server_default="ACTIVE"))
        op.execute("UPDATE companies SET status = 'SUSPENDED' WHERE is_active = 0")
    if not _has_column("companies", "plan"):
        with op.batch_alter_table("companies") as batch:
            batch.add_column(sa.Column("plan", sa.String(30), nullable=False,
                                       server_default="FREE"))

    # ── 2. platform_errors ─────────────────────────────────────────────
    if not _has_table("platform_errors"):
        op.create_table(
            "platform_errors",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"), nullable=True, index=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True, index=True),
            sa.Column("route", sa.String(255)),
            sa.Column("method", sa.String(10)),
            sa.Column("status_code", sa.Integer()),
            sa.Column("message", sa.Text()),
            sa.Column("traceback", sa.Text()),
            sa.Column("ip_address", sa.String(45)),
            sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        )


def downgrade():
    if _has_table("platform_errors"):
        op.drop_table("platform_errors")
    for col in ("plan", "status"):
        if _has_column("companies", col):
            with op.batch_alter_table("companies") as batch:
                batch.drop_column(col)
