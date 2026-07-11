"""MARSOUD-TASK-SCHEDULE — task_schedules + task_schedule_assignees.

Powers two tickets from Rofida via Abdelhamid:
  · schedule a task to fire on a future date (recurrence=ONCE)
  · repeat a task daily until an end date  (recurrence=DAILY)

Revision ID: a6b9c2d5e8f1
Revises: z5b8e4d9c2a6
Create Date: 2026-07-11
"""
from alembic import op
import sqlalchemy as sa


revision = 'a6b9c2d5e8f1'
down_revision = 'f1b8a3d5e0c9'
branch_labels = None
depends_on = None


def _has_table(name):
    insp = sa.inspect(op.get_bind())
    return name in insp.get_table_names()


def upgrade():
    if not _has_table("task_schedules"):
        op.create_table(
            "task_schedules",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("priority", sa.String(length=20),
                      nullable=False, server_default="MEDIUM"),
            sa.Column("project_id", sa.Integer(),
                      sa.ForeignKey("projects.id", ondelete="CASCADE"),
                      nullable=True),
            sa.Column("milestone_id", sa.Integer(),
                      sa.ForeignKey("milestones.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("assigned_to_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=False),
            sa.Column("recurrence", sa.String(length=20),
                      nullable=False, server_default="ONCE"),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=True),
            sa.Column("active", sa.Boolean(),
                      nullable=False, server_default=sa.text("1")),
            sa.Column("last_generated_date", sa.Date(), nullable=True),
            sa.Column("generated_count", sa.Integer(),
                      nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(),
                      nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_task_schedules_company_id",
                        "task_schedules", ["company_id"])
        op.create_index("ix_task_schedules_active",
                        "task_schedules", ["active"])

    if not _has_table("task_schedule_assignees"):
        op.create_table(
            "task_schedule_assignees",
            sa.Column("schedule_id", sa.Integer(),
                      sa.ForeignKey("task_schedules.id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id"), primary_key=True),
        )


def downgrade():
    if _has_table("task_schedule_assignees"):
        op.drop_table("task_schedule_assignees")
    if _has_table("task_schedules"):
        op.drop_index("ix_task_schedules_active",
                      table_name="task_schedules")
        op.drop_index("ix_task_schedules_company_id",
                      table_name="task_schedules")
        op.drop_table("task_schedules")
