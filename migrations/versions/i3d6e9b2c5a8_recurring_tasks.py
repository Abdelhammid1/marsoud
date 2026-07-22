"""MARSOUD-RECURRING-TASKS (Abdelhamid 2026-07-22).

Convert any existing Task to a recurring series. Distinct from
TaskSchedule (which is a template-based scheduler) — here the user
takes an existing Task, clicks "make recurring", and every future
occurrence is generated as a NEW Task that carries the series link.

Schema:
  · recurring_task_series — frequency + end condition + exception dates
    lookup.
  · recurring_task_exceptions — dates to skip in the generation cycle.
  · tasks.recurring_series_id + tasks.occurrence_index — links each
    generated Task back to its series and remembers which occurrence
    it is (for edit modes "this + future" / "all").

Revision ID: i3d6e9b2c5a8
Revises: h2c5e8a1b4d7
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'i3d6e9b2c5a8'
down_revision = 'h2c5e8a1b4d7'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "recurring_task_series" not in insp.get_table_names():
        op.create_table(
            "recurring_task_series",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"), nullable=False,
                      index=True),
            # The template Task the series was created FROM. Used to
            # copy fields (assignee, project, priority) onto each new
            # occurrence.
            sa.Column("template_task_id", sa.Integer(),
                      sa.ForeignKey("tasks.id"), nullable=False),
            sa.Column("frequency", sa.String(20), nullable=False),
            # Only meaningful for CUSTOM; other frequencies infer their
            # interval from the type (DAILY=1, WEEKLY=7, etc).
            sa.Column("interval_count", sa.Integer(),
                      nullable=False, server_default="1"),
            sa.Column("end_condition", sa.String(20),
                      nullable=False, server_default="NEVER"),
            sa.Column("end_count", sa.Integer(), nullable=True),
            sa.Column("end_date", sa.Date(), nullable=True),
            # Anchor date — first generated occurrence uses this date;
            # every next occurrence is (last + step).
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("last_generated_date", sa.Date(), nullable=True),
            sa.Column("generated_count", sa.Integer(),
                      nullable=False, server_default="0"),
            sa.Column("active", sa.Boolean(),
                      nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
        )

    if "recurring_task_exceptions" not in insp.get_table_names():
        op.create_table(
            "recurring_task_exceptions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("series_id", sa.Integer(),
                      sa.ForeignKey("recurring_task_series.id",
                                    ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("skip_date", sa.Date(), nullable=False),
            sa.UniqueConstraint(
                "series_id", "skip_date",
                name="uq_recurring_task_exception"),
        )

    if not _has_col("tasks", "recurring_series_id"):
        with op.batch_alter_table("tasks", schema=None) as batch:
            batch.add_column(sa.Column(
                "recurring_series_id", sa.Integer(),
                sa.ForeignKey("recurring_task_series.id",
                              name="fk_tasks_recurring_series_id"),
                nullable=True))
            batch.add_column(sa.Column(
                "occurrence_index", sa.Integer(), nullable=True))
            batch.create_index(
                "ix_tasks_recurring_series",
                ["recurring_series_id"])


def downgrade():
    insp = sa.inspect(op.get_bind())
    if _has_col("tasks", "recurring_series_id"):
        with op.batch_alter_table("tasks", schema=None) as batch:
            try: batch.drop_index("ix_tasks_recurring_series")
            except Exception: pass
            batch.drop_column("occurrence_index")
            batch.drop_column("recurring_series_id")
    if "recurring_task_exceptions" in insp.get_table_names():
        op.drop_table("recurring_task_exceptions")
    if "recurring_task_series" in insp.get_table_names():
        op.drop_table("recurring_task_series")
