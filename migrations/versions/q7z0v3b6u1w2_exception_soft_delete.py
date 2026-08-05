"""MARSOUD-EXCEPTION-AUDIT (2026-08-05) — cancelling instead of deleting.

delete_exception() did a hard db.session.delete(): the row vanished with
no record of who removed it or why. An attendance exception is money —
it deducts a day's pay — so removing one silently is exactly the kind of
change an audit needs to be able to see.

The row now stays and is stamped cancelled. Every query that FEEDS
PAYROLL must exclude it, or a cancelled exception keeps costing the
employee — that sweep is the load-bearing half of this change, not the
columns.

Additive: four nullable columns plus a boolean defaulting to false, so
every existing row reads as "not cancelled" and nothing changes.

WHY NOT A PLAIN ADD_COLUMN FOR cancelled_by. It carries a ForeignKey,
and SQLite answers "No support for ALTER of constraints in SQLite
dialect": the column lands, the migration aborts, and the version is
left unstamped — the exact half-applied wreckage this codebase has hit
before.

WHY NOT BATCH MODE EITHER. batch_alter_table rebuilds the table, and
this one carries an UNNAMED CHECK constraint generated from the
`type` Enum column. Batch refuses it with "Constraint must have a name",
and a naming_convention does not reach a reflected CHECK.

So: four plain columns on every backend, and the ForeignKey added
separately only where the backend can express it. SQLite ends up with
`cancelled_by` as a plain integer, which costs nothing — PRAGMA
foreign_keys is 0 in this app, so no FK on this table is enforced there
anyway. Postgres, which is what production runs, gets the real
constraint.

Every add is guarded, so a run that died half way through completes on
the next pass rather than failing on a column that already exists.

Revision ID: q7z0v3b6u1w2
Revises: p6y9u2a5t0v1
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'q7z0v3b6u1w2'
down_revision = 'p6y9u2a5t0v1'
branch_labels = None
depends_on = None

TABLE = "attendance_exceptions"
COLS = ("is_cancelled", "cancelled_by", "cancelled_at", "cancel_reason")
FK_NAME = "fk_attendance_exceptions_cancelled_by_users"


def _cols():
    insp = sa.inspect(op.get_bind())
    if TABLE not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(TABLE)}


def upgrade():
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    have = _cols()
    if "is_cancelled" not in have:
        op.add_column(TABLE, sa.Column("is_cancelled", sa.Boolean,
                                       nullable=False,
                                       server_default=sa.false()))
    if "cancelled_by" not in have:
        op.add_column(TABLE, sa.Column("cancelled_by", sa.Integer))
    if "cancelled_at" not in have:
        op.add_column(TABLE, sa.Column("cancelled_at", sa.DateTime))
    if "cancel_reason" not in have:
        op.add_column(TABLE, sa.Column("cancel_reason", sa.Text))

    # The constraint itself, where the backend can take it.
    if bind.dialect.name == "postgresql":
        existing = {fk["name"] for fk
                    in sa.inspect(bind).get_foreign_keys(TABLE)}
        if FK_NAME not in existing:
            op.create_foreign_key(FK_NAME, TABLE, "users",
                                  ["cancelled_by"], ["id"])


def downgrade():
    bind = op.get_bind()
    have = _cols()
    if bind.dialect.name == "postgresql":
        try:
            op.drop_constraint(FK_NAME, TABLE, type_="foreignkey")
        except Exception:
            pass
    for c in COLS:
        if c in have:
            op.drop_column(TABLE, c)
