"""MARSOUD-VBILL-OVERDUE-01 (2026-08-06) — vendor bills stay visible
until someone acts on them.

Two related problems this migration underpins:

  1. A recurring vendor-bill forecast used to disappear the moment its
     projected date passed — the panel showed 7 days out, the row aged
     off, and no record existed anywhere. HR concluded "must have been
     paid" and moved on. Now the cron materialises the forecast into
     a real POSTED VendorBill on the day; the two new columns
     `recurring_bill_id` + `recurring_occurrence_date` are what let
     the cron know it already made this one, so a double-firing run
     cannot double-post.

  2. Real posted vendor bills only flipped to OVERDUE when someone
     opened the vendor-bills index page. The cron now does it too.
     No schema change needed for that half — this migration is only
     for the tracking columns.

Additive. All columns nullable, so every existing row reads as
"not materialised from a forecast, not postponed". Fresh dev + prod
migrate cleanly.

WHY THE UNIQUE INDEX IS SAFE FOR STANDALONE BILLS. Both SQLite and
Postgres treat NULL as distinct in UNIQUE, so any number of
hand-entered bills — where recurring_bill_id is NULL — can coexist.
The constraint only bites the two-non-null case, which is exactly
where we want it to bite (one materialised bill per (template, date)).

Revision ID: s1p6a3d7v9b4
Revises: r0y5c8v2n1p3
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa


revision = 's1p6a3d7v9b4'
down_revision = 'r0y5c8v2n1p3'
branch_labels = None
depends_on = None


TABLE = "vendor_bills"
COLS = (
    "recurring_bill_id",
    "recurring_occurrence_date",
    "previous_due_date",
    "postpone_reason",
    "postponed_by",
    "postponed_at",
)
UNIQ_NAME = "uq_vendor_bill_recurring_occurrence"
FK_REC_NAME = "fk_vendor_bill_recurring_bill"
FK_POSTPONER_NAME = "fk_vendor_bill_postponed_by_users"


def _cols():
    insp = sa.inspect(op.get_bind())
    if TABLE not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(TABLE)}


def _indexes():
    insp = sa.inspect(op.get_bind())
    if TABLE not in insp.get_table_names():
        return set()
    return {i["name"] for i in insp.get_indexes(TABLE)}


def upgrade():
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return

    have = _cols()
    if "recurring_bill_id" not in have:
        op.add_column(TABLE, sa.Column(
            "recurring_bill_id", sa.Integer, nullable=True))
    if "recurring_occurrence_date" not in have:
        op.add_column(TABLE, sa.Column(
            "recurring_occurrence_date", sa.Date, nullable=True))
    if "previous_due_date" not in have:
        op.add_column(TABLE, sa.Column(
            "previous_due_date", sa.Date, nullable=True))
    if "postpone_reason" not in have:
        op.add_column(TABLE, sa.Column(
            "postpone_reason", sa.Text, nullable=True))
    if "postponed_by" not in have:
        op.add_column(TABLE, sa.Column(
            "postponed_by", sa.Integer, nullable=True))
    if "postponed_at" not in have:
        op.add_column(TABLE, sa.Column(
            "postponed_at", sa.DateTime, nullable=True))

    # Unique index on (recurring_bill_id, recurring_occurrence_date).
    # NULLs in either column exempt the row (SQLite + Postgres both
    # treat NULL as distinct in UNIQUE), so hand-entered bills remain
    # unconstrained; only two-non-null rows are enforced unique.
    if UNIQ_NAME not in _indexes():
        op.create_index(UNIQ_NAME, TABLE,
                        ["recurring_bill_id", "recurring_occurrence_date"],
                        unique=True)

    # Foreign keys — Postgres only. SQLite ignores FK on alter (fact 4
    # from the attendance handoff), and PRAGMA foreign_keys is off in
    # this app anyway.
    if bind.dialect.name == "postgresql":
        existing_fks = {fk["name"] for fk
                        in sa.inspect(bind).get_foreign_keys(TABLE)}
        if FK_REC_NAME not in existing_fks:
            op.create_foreign_key(FK_REC_NAME, TABLE, "recurring_bills",
                                  ["recurring_bill_id"], ["id"])
        if FK_POSTPONER_NAME not in existing_fks:
            op.create_foreign_key(FK_POSTPONER_NAME, TABLE, "users",
                                  ["postponed_by"], ["id"])


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name in (FK_REC_NAME, FK_POSTPONER_NAME):
            try:
                op.drop_constraint(name, TABLE, type_="foreignkey")
            except Exception:
                pass
    if UNIQ_NAME in _indexes():
        op.drop_index(UNIQ_NAME, table_name=TABLE)
    have = _cols()
    for c in COLS:
        if c in have:
            try:
                op.drop_column(TABLE, c)
            except Exception:
                pass
