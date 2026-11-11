"""CRM: lead_quotation_meta (status + note per uploaded quotation)  # MARSOUD-CRM-P18

Revision ID: f7a2c9d4b1e6
Revises: c4d9a1f6e3b7
Create Date: 2026-11-11 07:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "f7a2c9d4b1e6"
down_revision = "c4d9a1f6e3b7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "lead_quotation_meta",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="SENT"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.UniqueConstraint("document_id", name="uq_lead_quotation_meta_document"),
    )
    op.create_index("ix_lead_quotation_meta_company_id", "lead_quotation_meta", ["company_id"])
    op.create_index("ix_lead_quotation_meta_lead_id", "lead_quotation_meta", ["lead_id"])


def downgrade():
    op.drop_index("ix_lead_quotation_meta_lead_id", table_name="lead_quotation_meta")
    op.drop_index("ix_lead_quotation_meta_company_id", table_name="lead_quotation_meta")
    op.drop_table("lead_quotation_meta")
