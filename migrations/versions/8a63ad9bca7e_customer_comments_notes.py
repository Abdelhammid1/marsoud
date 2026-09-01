"""MARSOUD-TKT-CUSTOMER-COMMENTS-NOTES — customer_comments +
customer_notes tables.

Two mirror-image tables for the tenant customer detail page:
  * customer_comments — thread-style discussion, mirrors
    task_comments in shape so the UI can be a direct copy.
  * customer_notes    — free-text log; each row is one note with
    author + timestamp, no threading.

Both are internal (owner/admin/accountant only) and never surface
on the customer portal.

Revision ID: 8a63ad9bca7e
Revises: 5e44c9a13a1c
Create Date: 2026-08-31

"""
from alembic import op
import sqlalchemy as sa

revision = "8a63ad9bca7e"
down_revision = "5e44c9a13a1c"
branch_labels = None
depends_on = None


def _has_table(name):
    insp = sa.inspect(op.get_bind())
    return name in insp.get_table_names()


def upgrade():
    if not _has_table("customer_comments"):
        op.create_table(
            "customer_comments",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("customer_id", sa.Integer,
                      sa.ForeignKey("customers.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=False),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.current_timestamp(),
                      nullable=False),
        )
    if not _has_table("customer_notes"):
        op.create_table(
            "customer_notes",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("customer_id", sa.Integer,
                      sa.ForeignKey("customers.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=False),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.current_timestamp(),
                      nullable=False),
        )


def downgrade():
    if _has_table("customer_notes"):
        op.drop_table("customer_notes")
    if _has_table("customer_comments"):
        op.drop_table("customer_comments")
