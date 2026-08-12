"""MARSOUD-APPROVAL-GATED-SUPERADMIN (2026-08-12) — add
users.requires_approval + pending_superadmin_actions table.

Two schema additions in one migration:

  A) users.requires_approval Boolean NOT NULL DEFAULT FALSE
     — flag on User. When True, every write attempt under
     `superadmin.*` is intercepted at the shared
     @superadmin_required decorator and queued in
     pending_superadmin_actions instead of executing.

  B) pending_superadmin_actions table — the queue itself.
     One row per intercepted request. Stores endpoint,
     method, url_path, view_args (JSON), form_data (JSON),
     staged_files (JSON of paths under static/staging/),
     status (pending/approved/rejected), actor + decider +
     timestamps.

Both additions are idempotent — _has_col + _has_table
guards so a rerun is a no-op. batch_alter_table for
SQLite's rebuild path. server_default on requires_approval
is set at add-column time then dropped so future INSERTs
must be explicit (matches the enum-scope + override-scope
migration patterns already in the tree).

Revision ID: h4i7j0k3l6m9
Revises: g3h6i9j2k5l8
Create Date: 2026-08-12
"""
from alembic import op
import sqlalchemy as sa


revision = "h4i7j0k3l6m9"
down_revision = "g3h6i9j2k5l8"
branch_labels = None
depends_on = None


TABLE_PENDING = "pending_superadmin_actions"
COL_REQ_APPR = "requires_approval"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_col(table, col):
    return col in {c["name"] for c in _inspector().get_columns(table)}


def upgrade():
    # (A) users.requires_approval
    if not _has_col("users", COL_REQ_APPR):
        with op.batch_alter_table("users") as batch:
            batch.add_column(sa.Column(
                COL_REQ_APPR, sa.Boolean,
                nullable=False, server_default="0",
            ))
        # Drop the default so future INSERTs are explicit.
        with op.batch_alter_table("users") as batch:
            batch.alter_column(COL_REQ_APPR,
                                server_default=None)

    # (B) pending_superadmin_actions
    if not _has_table(TABLE_PENDING):
        op.create_table(
            TABLE_PENDING,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("actor_id", sa.Integer,
                       sa.ForeignKey("users.id",
                                      ondelete="RESTRICT",
                                      name="fk_pending_action_actor"),
                       nullable=False, index=True),
            sa.Column("endpoint", sa.String(120),
                       nullable=False, index=True),
            sa.Column("method", sa.String(8), nullable=False),
            sa.Column("url_path", sa.String(500), nullable=False),
            sa.Column("view_args", sa.Text, nullable=True),
            sa.Column("form_data", sa.Text, nullable=True),
            sa.Column("staged_files", sa.Text, nullable=True),
            sa.Column("status", sa.String(16), nullable=False,
                       server_default="pending", index=True),
            sa.Column("created_at", sa.DateTime,
                       server_default=sa.func.current_timestamp(),
                       nullable=False, index=True),
            sa.Column("decided_by", sa.Integer,
                       sa.ForeignKey("users.id",
                                      name="fk_pending_action_decider"),
                       nullable=True),
            sa.Column("decided_at", sa.DateTime, nullable=True),
            sa.Column("decision_note", sa.Text, nullable=True),
            sa.CheckConstraint(
                "status IN ('pending','approved','rejected')",
                name="ck_pending_action_status"),
        )


def downgrade():
    if _has_table(TABLE_PENDING):
        op.drop_table(TABLE_PENDING)
    if _has_col("users", COL_REQ_APPR):
        with op.batch_alter_table("users") as batch:
            batch.drop_column(COL_REQ_APPR)
