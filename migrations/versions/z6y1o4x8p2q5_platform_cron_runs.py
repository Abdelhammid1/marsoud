"""MARSOUD-SUPERADMIN-CONTROL-01 T11 (2026-08-08) — platform_cron_runs.

One row per (job_name, tick) written by track_cron_job in
app/services/cron_tracking.py so the ops-health page can answer
"did the cron actually run?" after a restart.

Idempotent _has_table guard mirrors migrations/versions/
y7w0g9j3b5h0_item_custody.py:47-53 for safe re-runs / partial
migration histories.

Revision ID: z6y1o4x8p2q5
Revises: y7w0g9j3b5h0
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa


revision = 'z6y1o4x8p2q5'
down_revision = 'y7w0g9j3b5h0'
branch_labels = None
depends_on = None


TABLE = "platform_cron_runs"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_index(table, name):
    return any(ix["name"] == name
               for ix in _inspector().get_indexes(table))


def upgrade():
    if not _has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("job_name", sa.String(80), nullable=False),
            sa.Column("started_at", sa.DateTime, nullable=False),
            sa.Column("finished_at", sa.DateTime),
            sa.Column("status", sa.String(10), nullable=False,
                       server_default="ok"),
            sa.Column("summary_json", sa.Text),
            sa.Column("error_message", sa.Text),
        )
    if _has_table(TABLE) and not _has_index(TABLE, "ix_platform_cron_runs_job_name"):
        op.create_index("ix_platform_cron_runs_job_name",
                         TABLE, ["job_name"])
    if _has_table(TABLE) and not _has_index(TABLE, "ix_platform_cron_runs_started_at"):
        op.create_index("ix_platform_cron_runs_started_at",
                         TABLE, ["started_at"])


def downgrade():
    if _has_table(TABLE):
        try:
            op.drop_index("ix_platform_cron_runs_started_at",
                           table_name=TABLE)
        except Exception:
            pass
        try:
            op.drop_index("ix_platform_cron_runs_job_name",
                           table_name=TABLE)
        except Exception:
            pass
        op.drop_table(TABLE)
