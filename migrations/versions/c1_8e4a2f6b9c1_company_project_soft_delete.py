"""Soft-delete for companies + projects.

  companies.deleted_at        DateTime nullable, indexed
  companies.deleted_by_id     FK users.id nullable
  companies.deletion_reason   Text nullable

  projects.deleted_at         DateTime nullable, indexed
  projects.deleted_by_id      FK users.id nullable
  projects.deletion_reason    Text nullable

Owners soft-delete their own company; super-admins can soft-delete +
restore + permanently wipe via a second action. Owners can also
soft-delete projects (Ticket L) with the same restore path under
super-admin control.

Revision ID: c1_8e4a2f6b9c1
Revises: c0_7d3b9e2f4a1
Create Date: 2026-06-27
"""
from alembic import op
import sqlalchemy as sa

revision = 'c1_8e4a2f6b9c1'
down_revision = 'c0_7d3b9e2f4a1'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def _has_index(table, name):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return name in {i["name"] for i in insp.get_indexes(table)}


def upgrade():
    # Companies
    if not _has_col("companies", "deleted_at"):
        with op.batch_alter_table("companies", schema=None) as batch:
            batch.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))
    if not _has_col("companies", "deleted_by_id"):
        with op.batch_alter_table("companies", schema=None) as batch:
            batch.add_column(sa.Column("deleted_by_id", sa.Integer(), nullable=True))
    if not _has_col("companies", "deletion_reason"):
        with op.batch_alter_table("companies", schema=None) as batch:
            batch.add_column(sa.Column("deletion_reason", sa.Text(), nullable=True))
    if not _has_index("companies", "ix_companies_deleted_at"):
        op.create_index("ix_companies_deleted_at", "companies", ["deleted_at"])

    # Projects
    if not _has_col("projects", "deleted_at"):
        with op.batch_alter_table("projects", schema=None) as batch:
            batch.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))
    if not _has_col("projects", "deleted_by_id"):
        with op.batch_alter_table("projects", schema=None) as batch:
            batch.add_column(sa.Column("deleted_by_id", sa.Integer(), nullable=True))
    if not _has_col("projects", "deletion_reason"):
        with op.batch_alter_table("projects", schema=None) as batch:
            batch.add_column(sa.Column("deletion_reason", sa.Text(), nullable=True))
    if not _has_index("projects", "ix_projects_deleted_at"):
        op.create_index("ix_projects_deleted_at", "projects", ["deleted_at"])


def downgrade():
    if _has_index("projects", "ix_projects_deleted_at"):
        op.drop_index("ix_projects_deleted_at", table_name="projects")
    for col in ("deletion_reason", "deleted_by_id", "deleted_at"):
        if _has_col("projects", col):
            with op.batch_alter_table("projects", schema=None) as batch:
                batch.drop_column(col)
    if _has_index("companies", "ix_companies_deleted_at"):
        op.drop_index("ix_companies_deleted_at", table_name="companies")
    for col in ("deletion_reason", "deleted_by_id", "deleted_at"):
        if _has_col("companies", col):
            with op.batch_alter_table("companies", schema=None) as batch:
                batch.drop_column(col)
