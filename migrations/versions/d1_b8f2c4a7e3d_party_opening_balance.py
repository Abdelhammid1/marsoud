"""MARSOUD-PARTY-OPENING-BALANCE-01 — audit table for one-shot opening
balances captured when a customer or vendor is first added.

Revision ID: d1_b8f2c4a7e3d
Revises: c7_a9e1f4d7b2c
"""
from alembic import op
import sqlalchemy as sa


revision = "d1_b8f2c4a7e3d"
down_revision = "c7_a9e1f4d7b2c"
branch_labels = None
depends_on = None


def upgrade():
    if "party_opening_balances" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "party_opening_balances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(),
                  sa.ForeignKey("companies.id"),
                  nullable=False, index=True),
        sa.Column("party_type", sa.String(20), nullable=False, index=True),
        sa.Column("party_id", sa.Integer(), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(15, 4), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("journal_entry_id", sa.Integer(),
                  sa.ForeignKey("journal_entries.id"),
                  nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(),
                  server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint(
            "company_id", "party_type", "party_id",
            name="uq_party_opening_balance",
        ),
    )


def downgrade():
    if "party_opening_balances" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("party_opening_balances")
