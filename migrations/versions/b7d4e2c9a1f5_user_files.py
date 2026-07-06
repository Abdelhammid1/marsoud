"""MARSOUD-USER-FILES — private folder per user.

Adds the `user_files` table backing the "each user has a folder they
can put files in and preview without downloading" ticket. Rows are
scoped to (company_id, user_id); the streaming route enforces that
only the owner (or an admin with `users.view`) can read the bytes.

Revision ID: b7d4e2c9a1f5
Revises: a6c9d3e7b4f2
"""
from alembic import op
import sqlalchemy as sa


revision = "b7d4e2c9a1f5"
down_revision = "a6c9d3e7b4f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_files",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("company_id", sa.Integer,
                    sa.ForeignKey("companies.id"),
                    nullable=False, index=True),
        sa.Column("user_id", sa.Integer,
                    sa.ForeignKey("users.id"),
                    nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("storage_key", sa.String(400), nullable=False),
        sa.Column("mimetype", sa.String(120)),
        sa.Column("size_bytes", sa.Integer),
        sa.Column("created_at", sa.DateTime, nullable=False,
                    server_default=sa.func.now()),
    )
    op.create_index("ix_user_files_company_user",
                     "user_files", ["company_id", "user_id"])


def downgrade():
    op.drop_index("ix_user_files_company_user", table_name="user_files")
    op.drop_table("user_files")
