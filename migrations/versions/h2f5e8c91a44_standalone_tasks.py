"""MARSOUD-27 — allow tasks without a project (standalone tasks)

Revision ID: h2f5e8c91a44
Revises: g7d8b91e4a23
Create Date: 2026-06-10 12:00:00

Drops the NOT NULL constraint on tasks.project_id. A task can now exist
without a parent project — useful for ad-hoc work that doesn't fit into
the project hierarchy yet.
"""
from alembic import op
import sqlalchemy as sa


revision = "h2f5e8c91a44"
down_revision = "g7d8b91e4a23"
branch_labels = None
depends_on = None


def upgrade():
    # SQLite batch mode required for ALTER COLUMN
    with op.batch_alter_table("tasks") as batch:
        batch.alter_column("project_id", existing_type=sa.Integer, nullable=True)


def downgrade():
    # NOTE: this fails if any rows have project_id IS NULL. The downgrade
    # would only be safe after manually re-attaching every standalone task.
    with op.batch_alter_table("tasks") as batch:
        batch.alter_column("project_id", existing_type=sa.Integer, nullable=False)
