"""CRM: lead_options table + systems/industry/idea_stage/unqualified_reasons on leads

Revision ID: c4d9a1f6e3b7
Revises: e5f8c71b2a19
Create Date: 2026-11-11 03:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = "c4d9a1f6e3b7"
down_revision = "e5f8c71b2a19"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "lead_options",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.UniqueConstraint("company_id", "kind", "label",
                            name="uq_lead_options_company_kind_label"),
    )
    op.create_index("ix_lead_options_company_id", "lead_options", ["company_id"])
    op.create_index("ix_lead_options_kind", "lead_options", ["kind"])
    # plain ALTER TABLE ADD COLUMN (no table rebuild on SQLite)
    op.add_column("leads", sa.Column("systems_needed", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("unqualified_reasons", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("industry", sa.String(length=120), nullable=True))
    op.add_column("leads", sa.Column("idea_stage", sa.String(length=120), nullable=True))


def downgrade():
    with op.batch_alter_table("leads") as batch:
        batch.drop_column("idea_stage")
        batch.drop_column("industry")
        batch.drop_column("unqualified_reasons")
        batch.drop_column("systems_needed")
    op.drop_index("ix_lead_options_kind", table_name="lead_options")
    op.drop_index("ix_lead_options_company_id", table_name="lead_options")
    op.drop_table("lead_options")
