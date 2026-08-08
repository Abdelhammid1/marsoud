"""MARSOUD-SUPERADMIN-CONTROL-01 T4 (2026-08-08) — per-tenant
feature grant/deny overrides.

New table `company_feature_overrides` for the resolver's
step-2 hook (app/services/access.py::_company_override). One row
per (company_id, feature_code) with GRANT/DENY mode, required
reason, optional expires_at.

Additive; nothing existing touched. Idempotent via `_has_table`
guard so reruns are safe. Reason NOT NULL at the DB layer —
matches the ticket's 'السبب إجباري' rule literally even if a
direct-DB INSERT bypasses the service.

Revision ID: c1e2f3a4b5d6
Revises: y7w0g9j3b5h0
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa


revision = "c1e2f3a4b5d6"
down_revision = "y7w0g9j3b5h0"
branch_labels = None
depends_on = None


TABLE = "company_feature_overrides"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def upgrade():
    if _has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Integer,
                  sa.ForeignKey("companies.id", ondelete="CASCADE",
                                 name="fk_override_company_id"),
                  nullable=False, index=True),
        sa.Column("feature_code", sa.String(60),
                  nullable=False, index=True),
        sa.Column("mode", sa.String(8), nullable=False, index=True),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=True, index=True),
        sa.Column("created_by_id", sa.Integer,
                  sa.ForeignKey("users.id",
                                 name="fk_override_created_by_id"),
                  nullable=True),
        sa.Column("created_at", sa.DateTime,
                  server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("company_id", "feature_code",
                             name="uq_override_company_feature"),
        sa.CheckConstraint("mode IN ('GRANT', 'DENY')",
                            name="ck_override_mode"),
    )


def downgrade():
    if _has_table(TABLE):
        op.drop_table(TABLE)
