"""MARSOUD-CASH-CUSTODY-01 (2026-08-07) — cash custody tables.

Three new tables + two column additions:

  cash_custody_requests   — the request lifecycle
                            (PENDING → APPROVED / REJECTED)
  cash_custodies          — the live custody after issue
                            (ISSUED → PARTIALLY_SETTLED → SETTLED / CANCELLED)
  cash_custody_settlement_lines — one row per receipt during settlement

  employees.custody_account_id   — FK to accounts.id (per-employee
                                   1180 sub-account, minted lazily by
                                   subsidiary.ensure_custody_account)
  departments.custody_account_id — same for departments (holders can
                                   be either type per the ticket)

The CHECK constraint on holder columns is the "exactly one holder"
guarantee — no crafted POST can stitch both employee_id and
department_id, or leave both null. Both tables carry it.

Additive; no touches to existing tables. Reruns are safe via
`_has_table` / `_has_column` guards.

Revision ID: w5u8e7h1z3f8
Revises: v4t9d6g0y2e7
Create Date: 2026-08-07
"""
from alembic import op
import sqlalchemy as sa


revision = 'w5u8e7h1z3f8'
down_revision = 'x6v9f8i2a4g9'
branch_labels = None
depends_on = None


REQUESTS_TABLE = "cash_custody_requests"
CUSTODIES_TABLE = "cash_custodies"
LINES_TABLE = "cash_custody_settlement_lines"

# Same CHECK constraint SQL the model uses at
# app/models/cash_custody.py::_HOLDER_CHECK. Kept verbatim so the
# ORM-level and DB-level enforcement never drift.
_HOLDER_CHECK = (
    "(holder_type = 'EMPLOYEE' AND employee_id IS NOT NULL "
    "AND department_id IS NULL) "
    "OR "
    "(holder_type = 'DEPARTMENT' AND department_id IS NOT NULL "
    "AND employee_id IS NULL)"
)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_column(table, col):
    if not _has_table(table):
        return False
    return col in {c["name"] for c in _inspector().get_columns(table)}


def upgrade():
    # ─── cash_custody_requests ────────────────────────────────
    if not _has_table(REQUESTS_TABLE):
        op.create_table(
            REQUESTS_TABLE,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("holder_type", sa.String(16), nullable=False,
                      index=True),
            sa.Column("employee_id", sa.Integer,
                      sa.ForeignKey("employees.id", ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("department_id", sa.Integer,
                      sa.ForeignKey("departments.id",
                                    ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("amount", sa.Numeric(15, 2), nullable=False),
            sa.Column("purpose", sa.Text, nullable=False),
            sa.Column("needed_by_date", sa.Date, nullable=True),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="PENDING", index=True),
            sa.Column("reviewed_by", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("reviewed_at", sa.DateTime, nullable=True),
            sa.Column("review_note", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.current_timestamp()),
            sa.Column("created_by", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=True),
            sa.CheckConstraint(_HOLDER_CHECK,
                                name="ck_custody_request_one_holder"),
        )

    # ─── cash_custodies ───────────────────────────────────────
    if not _has_table(CUSTODIES_TABLE):
        op.create_table(
            CUSTODIES_TABLE,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("holder_type", sa.String(16), nullable=False,
                      index=True),
            sa.Column("employee_id", sa.Integer,
                      sa.ForeignKey("employees.id", ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("department_id", sa.Integer,
                      sa.ForeignKey("departments.id",
                                    ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("amount_issued", sa.Numeric(15, 2),
                      nullable=False),
            sa.Column("amount_settled", sa.Numeric(15, 2),
                      nullable=False, server_default="0"),
            sa.Column("amount_returned", sa.Numeric(15, 2),
                      nullable=False, server_default="0"),
            sa.Column("amount_shortfall", sa.Numeric(15, 2),
                      nullable=False, server_default="0"),
            sa.Column("status", sa.String(24), nullable=False,
                      server_default="ISSUED", index=True),
            sa.Column("payment_method_id", sa.Integer,
                      sa.ForeignKey("payment_methods.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("purpose", sa.Text, nullable=True),
            sa.Column("issued_on", sa.Date, nullable=False),
            sa.Column("settlement_due_date", sa.Date, nullable=True,
                      index=True),
            sa.Column("request_id", sa.Integer,
                      sa.ForeignKey("cash_custody_requests.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("journal_entry_id", sa.Integer,
                      sa.ForeignKey("journal_entries.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("settlement_journal_entry_id", sa.Integer,
                      sa.ForeignKey("journal_entries.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("reversal_entry_id", sa.Integer,
                      sa.ForeignKey("journal_entries.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("note", sa.Text, nullable=True),
            sa.Column("approved_by", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_by", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("settled_by", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("settled_at", sa.DateTime, nullable=True),
            sa.Column("cancelled_by", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("cancelled_at", sa.DateTime, nullable=True),
            sa.Column("cancel_reason", sa.Text, nullable=True),
            sa.Column("shortfall_disposition", sa.String(24),
                      nullable=True),
            sa.Column("custody_overdue_notified_at", sa.DateTime,
                      nullable=True),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.current_timestamp()),
            sa.CheckConstraint(_HOLDER_CHECK,
                                name="ck_custody_one_holder"),
        )

    # ─── cash_custody_settlement_lines ───────────────────────
    if not _has_table(LINES_TABLE):
        op.create_table(
            LINES_TABLE,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("custody_id", sa.Integer,
                      sa.ForeignKey("cash_custodies.id",
                                    ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("expense_account_id", sa.Integer,
                      sa.ForeignKey("accounts.id",
                                    ondelete="RESTRICT"),
                      nullable=False),
            sa.Column("amount", sa.Numeric(15, 2), nullable=False),
            sa.Column("receipt_note", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.current_timestamp()),
            sa.Column("created_by", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=True),
        )

    # ─── holder-side FKs on employees + departments ─────────
    # Named constraints so SQLite's batch_alter_table can move
    # them across the table-copy dance without "Constraint must
    # have a name" errors.
    if not _has_column("employees", "custody_account_id"):
        with op.batch_alter_table("employees") as bop:
            bop.add_column(sa.Column(
                "custody_account_id", sa.Integer,
                sa.ForeignKey("accounts.id",
                              name="fk_employees_custody_account"),
                nullable=True))
    if not _has_column("departments", "custody_account_id"):
        with op.batch_alter_table("departments") as bop:
            bop.add_column(sa.Column(
                "custody_account_id", sa.Integer,
                sa.ForeignKey("accounts.id",
                              name="fk_departments_custody_account"),
                nullable=True))


def downgrade():
    # Reverse column adds first so table drops don't fail on FKs.
    if _has_column("departments", "custody_account_id"):
        with op.batch_alter_table("departments") as bop:
            bop.drop_column("custody_account_id")
    if _has_column("employees", "custody_account_id"):
        with op.batch_alter_table("employees") as bop:
            bop.drop_column("custody_account_id")
    for tbl in (LINES_TABLE, CUSTODIES_TABLE, REQUESTS_TABLE):
        if _has_table(tbl):
            op.drop_table(tbl)
