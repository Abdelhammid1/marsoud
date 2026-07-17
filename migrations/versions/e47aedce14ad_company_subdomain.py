"""MARSOUD-SAAS-SUBDOMAIN (Abdelhamid 2026-07-17).

Adds companies.subdomain column to support per-tenant subdomains
(e.g. shalaby-store.marsoud.com). Nullable for now — existing
companies get backfilled in a follow-up data migration/script before
we make it NOT NULL. Unique + indexed since it's the tenant lookup key.

Revision ID: e47aedce14ad
Revises: e5c8f1a4b7d0
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa


revision = "e47aedce14ad"
down_revision = "e5c8f1a4b7d0"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("companies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("subdomain", sa.String(length=63), nullable=True))
        batch_op.create_index(
            "ix_companies_subdomain", ["subdomain"], unique=True
        )


def downgrade():
    with op.batch_alter_table("companies", schema=None) as batch_op:
        batch_op.drop_index("ix_companies_subdomain")
        batch_op.drop_column("subdomain")
