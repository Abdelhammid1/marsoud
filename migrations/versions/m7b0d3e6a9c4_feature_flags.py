"""MARSOUD-FEATURE-FLAGS-KILL-SWITCH (Abdelhamid 2026-07-22).

feature_flags table. Runtime module on/off from /admin/feature-flags
so a broken module (payroll / manufacturing / etc.) can be turned
OFF for every tenant without a redeploy.

Deliberately module-level only (not per-endpoint) — Abdelhamid's
ticket says "Feature Flags الخاصة بالمطورين" (per-endpoint /
experimental) are OUT of scope.

Revision ID: m7b0d3e6a9c4
Revises: l6a9c2e5b8d1
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'm7b0d3e6a9c4'
down_revision = 'l6a9c2e5b8d1'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "feature_flags" not in insp.get_table_names():
        op.create_table(
            "feature_flags",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("module_key", sa.String(60),
                      nullable=False, unique=True),
            sa.Column("enabled", sa.Boolean(),
                      nullable=False, server_default="1"),
            sa.Column("disabled_reason", sa.Text(), nullable=True),
            sa.Column("updated_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("updated_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "feature_flags" in insp.get_table_names():
        op.drop_table("feature_flags")
