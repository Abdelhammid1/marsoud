"""MARSOUD-METRIC-AUTOMATION (2026-08-05) — link a metric entry to its event.

MetricLogEntry had no reference to what caused it. Two things need one:

  · the ticket asks for the source event to be kept (requirement 7)
  · it is the natural idempotency key. The cron job walks
    UserActivityLog rows; without a unique link, a second tick would
    score the same event again, and "process only new events" would rest
    on a high-water mark that a retry or a clock change could undo.

Nullable on purpose: every entry logged by hand before today has no
source event, and manual logging stays supported unchanged.

The unique index is on (cycle_id, source_activity_id) rather than
source_activity_id alone — one event can legitimately score in two
different cycles if their date ranges overlap, and partial uniqueness
over NULLs behaves differently across backends, so the compound index
keeps manual rows (source NULL) out of each other's way.

Revision ID: l2u5q8w1p6r7
Revises: k1t4p7v0o5q6
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'l2u5q8w1p6r7'
down_revision = 'k1t4p7v0o5q6'
branch_labels = None
depends_on = None

TABLE = "metric_log_entries"
COL = "source_activity_id"
IDX = "uq_metric_entry_source_event"


def _cols(table):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _indexes(table):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {i["name"] for i in insp.get_indexes(table)}


def upgrade():
    if TABLE not in sa.inspect(op.get_bind()).get_table_names():
        return
    if COL not in _cols(TABLE):
        # No FK constraint: user_activity_log rows are prunable
        # housekeeping data, and a retention sweep must never be blocked
        # by a metric entry pointing at an old event.
        op.add_column(TABLE, sa.Column(COL, sa.Integer, nullable=True))
    if IDX not in _indexes(TABLE):
        op.create_index(IDX, TABLE, ["cycle_id", COL], unique=True)


def downgrade():
    if IDX in _indexes(TABLE):
        op.drop_index(IDX, table_name=TABLE)
    if COL in _cols(TABLE):
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(COL)
