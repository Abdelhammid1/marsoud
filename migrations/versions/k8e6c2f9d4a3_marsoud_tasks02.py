"""MARSOUD-TASKS-02 — task comments, activity log, multi-assignee

Revision ID: k8e6c2f9d4a3
Revises: j5b4d7a82e91
Create Date: 2026-06-11 14:00:00

Adds the three tables needed for the richer tasks module:

  - task_assignees       (task_id, user_id) — M2M, replaces the single
                          assigned_to_id (kept on Task as "primary"
                          assignee for backward-compat)
  - task_comments        (task_id, user_id, company_id, content,
                          attachment_url, created_at)
  - task_activity_logs   (task_id, user_id, company_id, action,
                          before_json, after_json, created_at)

Plus a backfill step: for every existing task with assigned_to_id set,
insert a matching task_assignees row so the multi-assignee query
returns the same set users previously saw.

Idempotent.
"""
from alembic import op
import sqlalchemy as sa


revision = "k8e6c2f9d4a3"
down_revision = "j5b4d7a82e91"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def upgrade():
    if not _has_table("task_assignees"):
        op.create_table(
            "task_assignees",
            sa.Column("task_id", sa.Integer,
                      sa.ForeignKey("tasks.id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"),
                      primary_key=True),
            sa.Column("assigned_by_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("assigned_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False),
        )

    if not _has_table("task_comments"):
        op.create_table(
            "task_comments",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("task_id", sa.Integer,
                      sa.ForeignKey("tasks.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"),
                      nullable=False),
            sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("attachment_url", sa.String(400)),
            sa.Column("attachment_name", sa.String(200)),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False),
        )

    if not _has_table("task_activity_logs"):
        op.create_table(
            "task_activity_logs",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("task_id", sa.Integer,
                      sa.ForeignKey("tasks.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("action", sa.String(60), nullable=False),
            sa.Column("before_json", sa.Text),
            sa.Column("after_json", sa.Text),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.now(), nullable=False),
        )

    # ── Backfill: copy tasks.assigned_to_id → task_assignees rows ───────
    conn = op.get_bind()
    if _has_table("task_assignees") and _has_table("tasks"):
        # Only insert rows that don't already exist (idempotent).
        conn.execute(sa.text("""
            INSERT INTO task_assignees (task_id, user_id, assigned_by_id)
            SELECT t.id, t.assigned_to_id, t.assigned_to_id
            FROM tasks t
            WHERE t.assigned_to_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM task_assignees ta
                WHERE ta.task_id = t.id AND ta.user_id = t.assigned_to_id
              )
        """))


def downgrade():
    for t in ("task_activity_logs", "task_comments", "task_assignees"):
        if _has_table(t):
            try:
                op.drop_table(t)
            except Exception:
                pass
