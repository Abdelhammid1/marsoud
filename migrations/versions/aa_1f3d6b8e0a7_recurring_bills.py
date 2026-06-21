"""Recurring vendor bills + per-occurrence overrides

  recurring_bills            template carrying vendor + amount + interval
  recurring_bill_overrides   per-date SKIP/AMEND for the template above

No journal entries are ever created by this module — it's a projection
layer only. The "real" vendor bill is the one the user manually posts
when it actually arrives.

Revision ID: aa_1f3d6b8e0a7
Revises: z5b8e4d9c2a6
Create Date: 2026-06-21
"""
from alembic import op
import sqlalchemy as sa

revision = 'aa_1f3d6b8e0a7'
down_revision = 'z5b8e4d9c2a6'
branch_labels = None
depends_on = None


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    if not _has_table("recurring_bills"):
        op.create_table(
            "recurring_bills",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("source_bill_id", sa.Integer(),
                      sa.ForeignKey("vendor_bills.id"),
                      nullable=False, index=True),
            sa.Column("vendor_id", sa.Integer(),
                      sa.ForeignKey("vendors.id"), nullable=True),
            sa.Column("amount", sa.Numeric(15, 4), nullable=False,
                      server_default="0"),
            sa.Column("currency", sa.String(3), nullable=False,
                      server_default="SAR"),
            sa.Column("interval_unit", sa.String(10), nullable=False),
            sa.Column("interval_count", sa.Integer(), nullable=False,
                      server_default="1"),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False,
                      server_default=sa.text("1")),
            sa.Column("created_by", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True,
                      server_default=sa.func.current_timestamp()),
        )
    if not _has_table("recurring_bill_overrides"):
        op.create_table(
            "recurring_bill_overrides",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("recurring_bill_id", sa.Integer(),
                      sa.ForeignKey("recurring_bills.id",
                                     ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("occurrence_date", sa.Date(), nullable=False),
            sa.Column("action", sa.String(10), nullable=False),
            sa.Column("amount", sa.Numeric(15, 4), nullable=True),
            sa.UniqueConstraint("recurring_bill_id", "occurrence_date",
                                name="uq_recurring_override_date"),
        )


def downgrade():
    if _has_table("recurring_bill_overrides"):
        op.drop_table("recurring_bill_overrides")
    if _has_table("recurring_bills"):
        op.drop_table("recurring_bills")
