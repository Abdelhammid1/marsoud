"""MARSOUD-32 — custom roles + granular permissions (lexoffice-style)

Revision ID: j5b4d7a82e91
Revises: i3c9f4d8e2b7
Create Date: 2026-06-11 12:00:00

Adds the data model the company owner needs to define custom roles and
manage permissions inside Marsoud — same shape as lexoffice's
/admin/settings/roles UI:

  - permissions          (catalog of every action: invoices.create,
                          journals.reverse, etc. — one row per existing
                          P-dict key, with resource/action/group metadata)
  - roles                (per-company role list — SYSTEM seeded from
                          existing ALL_ROLES, CUSTOM created by owner)
  - role_permissions     (M2M between roles and permissions)
  - user_companies.role_id  (new nullable FK alongside the existing
                          `role` string column — kept in sync during the
                          transition; reading still falls back to the
                          string when role_id is null)

Idempotent. Doesn't drop the existing user_companies.role column — that
backfill happens in the next data-migration step (see the seed service).
"""
from alembic import op
import sqlalchemy as sa


revision = "j5b4d7a82e91"
down_revision = "i3c9f4d8e2b7"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_column(table, column):
    if not _has_table(table):
        return False
    return any(c["name"] == column for c in _inspector().get_columns(table))


def upgrade():
    # ── permissions ─────────────────────────────────────────────────────
    if not _has_table("permissions"):
        op.create_table(
            "permissions",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("code", sa.String(80), nullable=False, unique=True, index=True),
            sa.Column("resource", sa.String(50), nullable=False, index=True),
            sa.Column("action", sa.String(50), nullable=False),
            sa.Column("group_ar", sa.String(80), nullable=False),
            sa.Column("label_ar", sa.String(120), nullable=False),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        )

    # ── roles ───────────────────────────────────────────────────────────
    if not _has_table("roles"):
        op.create_table(
            "roles",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("code", sa.String(50), nullable=False),
            sa.Column("name_ar", sa.String(120), nullable=False),
            sa.Column("type", sa.String(20), nullable=False, server_default="CUSTOM"),
            sa.Column("description", sa.Text),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("company_id", "code", name="uq_role_company_code"),
        )

    # ── role_permissions M2M ────────────────────────────────────────────
    if not _has_table("role_permissions"):
        op.create_table(
            "role_permissions",
            sa.Column("role_id", sa.Integer,
                      sa.ForeignKey("roles.id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("permission_id", sa.Integer,
                      sa.ForeignKey("permissions.id", ondelete="CASCADE"),
                      primary_key=True),
        )

    # ── user_companies.role_id (new FK alongside the existing string) ────
    if not _has_column("user_companies", "role_id"):
        with op.batch_alter_table("user_companies") as batch:
            batch.add_column(sa.Column("role_id", sa.Integer, nullable=True))


def downgrade():
    if _has_column("user_companies", "role_id"):
        with op.batch_alter_table("user_companies") as batch:
            try:
                batch.drop_column("role_id")
            except Exception:
                pass
    for t in ("role_permissions", "roles", "permissions"):
        if _has_table(t):
            try:
                op.drop_table(t)
            except Exception:
                pass
