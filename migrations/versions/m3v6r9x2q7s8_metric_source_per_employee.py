"""MARSOUD-METRIC-AUTOMATION — the source-event index must include the employee.

Found by auditing l2u5q8w1p6r7 rather than by using it.

That migration made (cycle_id, source_activity_id) unique. But ONE event
can legitimately produce SEVERAL entries: the ticket splits a task's
points across everyone assigned to it, and every one of those entries
comes from the same activity row. The second insert then dies with

    UNIQUE constraint failed:
    metric_log_entries.cycle_id, metric_log_entries.source_activity_id

so only the first assignee is ever credited and the failure is swallowed
as a skip. It was invisible because all four multi-assignee rules
(Task/Invoice/VendorBill/Customer) are «تحدد لاحقًا» and award 0 today —
the code path is never reached until someone sets a real number, which
is exactly when it would break.

The correct key is (cycle_id, source_activity_id, employee_id): one
event scores each employee at most once, which is what idempotency
actually means here.

Revision ID: m3v6r9x2q7s8
Revises: l2u5q8w1p6r7
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'm3v6r9x2q7s8'
down_revision = 'l2u5q8w1p6r7'
branch_labels = None
depends_on = None

TABLE = "metric_log_entries"
OLD = "uq_metric_entry_source_event"
NEW = "uq_metric_entry_source_event_emp"


def _indexes():
    insp = sa.inspect(op.get_bind())
    if TABLE not in insp.get_table_names():
        return set()
    return {i["name"] for i in insp.get_indexes(TABLE)}


def upgrade():
    have = _indexes()
    if not have and TABLE not in sa.inspect(op.get_bind()).get_table_names():
        return
    if OLD in have:
        op.drop_index(OLD, table_name=TABLE)
    if NEW not in _indexes():
        op.create_index(NEW, TABLE,
                        ["cycle_id", "source_activity_id", "employee_id"],
                        unique=True)


def downgrade():
    have = _indexes()
    if NEW in have:
        op.drop_index(NEW, table_name=TABLE)
    if OLD not in _indexes():
        op.create_index(OLD, TABLE, ["cycle_id", "source_activity_id"],
                        unique=True)
