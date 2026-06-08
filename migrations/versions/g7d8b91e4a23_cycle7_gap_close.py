"""Cycle 7 gap close — Documents + Notifications + ClientFeedback + AuditEntry + client role

Revision ID: g7d8b91e4a23
Revises: f1a4c9e23bd5
Create Date: 2026-06-08 18:00:00

Adds:
  - documents              (generic attachments: source_type in LEAD/PROJECT/TASK; visibility INTERNAL/CLIENT)
  - notifications          (in-app bell icon)
  - client_feedback        (1-5 rating + approved flag, gates project close)
  - audit_entries          (generic who/when/what for every edit)
  - users.linked_customer_id  (so a client User can be tied to a Customer)
  - leads.lost_reason already exists; no change needed there

All idempotent.
"""
from alembic import op
import sqlalchemy as sa


revision = "g7d8b91e4a23"
down_revision = "f1a4c9e23bd5"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_column(table, column):
    if not _has_table(table):
        return False
    return any(c["name"] == column for c in _inspector().get_columns(table))


def upgrade():
    if not _has_table("documents"):
        op.create_table(
            "documents",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("source_type", sa.String(20), nullable=False, index=True),
            sa.Column("source_id", sa.Integer, nullable=False, index=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("file_path", sa.String(400), nullable=False),
            sa.Column("mimetype", sa.String(100)),
            sa.Column("size_bytes", sa.Integer),
            sa.Column("visibility", sa.String(20), nullable=False,
                      server_default="INTERNAL"),
            sa.Column("uploaded_by_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False),
        )

    if not _has_table("notifications"):
        op.create_table(
            "notifications",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("kind", sa.String(40), nullable=False),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("body", sa.Text),
            sa.Column("link_url", sa.String(300)),
            sa.Column("read_at", sa.DateTime),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False, index=True),
        )

    if not _has_table("client_feedback"):
        op.create_table(
            "client_feedback",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("project_id", sa.Integer,
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("customer_id", sa.Integer, sa.ForeignKey("customers.id"),
                      nullable=False),
            sa.Column("submitted_by_user_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("rating", sa.Integer, nullable=False),
            sa.Column("comment", sa.Text),
            sa.Column("approved", sa.Boolean, nullable=False,
                      server_default=sa.false()),
            sa.Column("submitted_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False),
        )

    if not _has_table("audit_entries"):
        op.create_table(
            "audit_entries",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("entity_type", sa.String(40), nullable=False, index=True),
            sa.Column("entity_id", sa.Integer, nullable=False, index=True),
            sa.Column("action", sa.String(20), nullable=False),
            sa.Column("changed_by_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("changes_json", sa.Text),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now(),
                      nullable=False, index=True),
        )

    with op.batch_alter_table("users") as batch:
        if not _has_column("users", "linked_customer_id"):
            batch.add_column(sa.Column("linked_customer_id", sa.Integer, nullable=True))


def downgrade():
    with op.batch_alter_table("users") as batch:
        if _has_column("users", "linked_customer_id"):
            try:
                batch.drop_column("linked_customer_id")
            except Exception:
                pass
    for t in ("audit_entries", "client_feedback", "notifications", "documents"):
        if _has_table(t):
            try:
                op.drop_table(t)
            except Exception:
                pass
