"""MARSOUD-CHOOSE-PLAN (Abdelhamid 2026-07-22).

Adds companies.intended_plan_id FK plans.id NULL. Signup no longer
auto-assigns Enterprise — new companies get NULL here, get all
features during the trial, and see /choose-plan after email verify.
Once they pick a plan, this column is stamped and plan_gating
starts enforcing after the trial window.

Revision ID: f0a3c6e9b4d2
Revises: e9f2a5c8b1d4
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'f0a3c6e9b4d2'
down_revision = 'e9f2a5c8b1d4'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("companies", "intended_plan_id"):
        with op.batch_alter_table("companies", schema=None) as batch:
            batch.add_column(sa.Column(
                "intended_plan_id", sa.Integer(),
                sa.ForeignKey("plans.id",
                              name="fk_companies_intended_plan_id"),
                nullable=True))


def downgrade():
    if _has_col("companies", "intended_plan_id"):
        with op.batch_alter_table("companies", schema=None) as batch:
            batch.drop_column("intended_plan_id")
