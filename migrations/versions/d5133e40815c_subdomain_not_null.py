"""MARSOUD-SAAS-SUBDOMAIN-2 (Abdelhamid 2026-07-17).

Locks companies.subdomain to NOT NULL now that all existing
companies have been backfilled. New companies must set it at
creation time going forward.

Revision ID: d5133e40815c
Revises: e47aedce14ad
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa


revision = "d5133e40815c"
down_revision = "e47aedce14ad"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("companies", schema=None) as batch_op:
        batch_op.alter_column(
            "subdomain",
            existing_type=sa.String(length=63),
            nullable=False,
        )


def downgrade():
    with op.batch_alter_table("companies", schema=None) as batch_op:
        batch_op.alter_column(
            "subdomain",
            existing_type=sa.String(length=63),
            nullable=True,
        )
