"""super admin layer (Cycle 4)

Revision ID: a4e1d2c80042
Revises: 5717814e0264
Create Date: 2026-06-05 12:00:00

Idempotent: each schema change is gated on a fresh inspector probe so the
migration can be re-run against a partially-migrated DB.

Adds:
  - users.is_superadmin           (BOOLEAN, default 0)
  - users.last_login_at           (DATETIME, nullable)
  - users.is_active               (BOOLEAN, default 1)
  - platform_audit_logs           (cross-company activity log)
  - superadmin_impersonations     (view-as audit trail)
"""
from alembic import op
import sqlalchemy as sa


revision = "a4e1d2c80042"
down_revision = "5717814e0264"
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
    # ── 1. users.is_superadmin / last_login_at / is_active ──────────────
    if not _has_column("users", "is_superadmin"):
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column(
                "is_superadmin", sa.Boolean(), nullable=False,
                server_default=sa.false(),
            ))
    if not _has_column("users", "last_login_at"):
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column("last_login_at", sa.DateTime(), nullable=True))
    if not _has_column("users", "is_active"):
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column(
                "is_active", sa.Boolean(), nullable=False,
                server_default=sa.true(),
            ))

    # ── 2. platform_audit_logs ──────────────────────────────────────────
    if not _has_table("platform_audit_logs"):
        op.create_table(
            "platform_audit_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("actor_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True, index=True),
            sa.Column("action", sa.String(60), nullable=False, index=True),
            sa.Column("target_company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"), nullable=True, index=True),
            sa.Column("target_user_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True, index=True),
            sa.Column("ip_address", sa.String(45), nullable=True),
            sa.Column("details", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        )

    # ── 3. superadmin_impersonations ────────────────────────────────────
    if not _has_table("superadmin_impersonations"):
        op.create_table(
            "superadmin_impersonations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("superadmin_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=False, index=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"), nullable=False, index=True),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("ended_at", sa.DateTime(), nullable=True),
            sa.Column("ip_address", sa.String(45), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
        )


def downgrade():
    if _has_table("superadmin_impersonations"):
        op.drop_table("superadmin_impersonations")
    if _has_table("platform_audit_logs"):
        op.drop_table("platform_audit_logs")
    for col in ("is_active", "last_login_at", "is_superadmin"):
        if _has_column("users", col):
            with op.batch_alter_table("users") as batch:
                batch.drop_column(col)
