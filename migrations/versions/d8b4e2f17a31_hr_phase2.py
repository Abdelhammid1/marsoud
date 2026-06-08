"""HR Phase 2 — leave types, balances, attendance exceptions, leave requests (Cycle 6)

Revision ID: d8b4e2f17a31
Revises: c7a3f1e0b257
Create Date: 2026-06-08 12:00:00

Adds:
  - leave_types               (HR-05)
  - leave_balances            (HR-05)
  - leave_requests            (HR-06)
  - attendance_exceptions     (HR-05b)
  - payroll_lines.attendance_auto_calculated  (HR-07)

All changes are additive and idempotent — each guarded by an inspector probe
so the migration can be re-run safely against a partially-migrated DB.

Note on table ordering: attendance_exceptions has a nullable FK to
leave_requests, so leave_requests must be created first.
"""
from alembic import op
import sqlalchemy as sa


revision = "d8b4e2f17a31"
down_revision = "c7a3f1e0b257"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_column(table, column):
    if not _has_table(table):
        return False
    return any(c["name"] == column for c in _inspector().get_columns(table))


def upgrade():
    # ─── leave_types ─────────────────────────────────────────────────────
    if not _has_table("leave_types"):
        op.create_table(
            "leave_types",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("accrual_per_month", sa.Numeric(6, 3), nullable=False, server_default="0"),
            sa.Column("max_balance", sa.Numeric(6, 2), nullable=False, server_default="0"),
            sa.Column("is_paid", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
            sa.UniqueConstraint("company_id", "name", name="uq_leave_type_company_name"),
        )

    # ─── leave_balances ──────────────────────────────────────────────────
    if not _has_table("leave_balances"):
        op.create_table(
            "leave_balances",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("employee_id", sa.Integer, sa.ForeignKey("employees.id"),
                      nullable=False, index=True),
            sa.Column("leave_type_id", sa.Integer, sa.ForeignKey("leave_types.id"),
                      nullable=False, index=True),
            sa.Column("year", sa.Integer, nullable=False),
            sa.Column("balance_days", sa.Numeric(7, 2), nullable=False, server_default="0"),
            sa.Column("used_days", sa.Numeric(7, 2), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
            sa.UniqueConstraint("employee_id", "leave_type_id", "year",
                                name="uq_leave_balance_employee_type_year"),
        )

    # ─── leave_requests (must precede attendance_exceptions for the FK) ──
    if not _has_table("leave_requests"):
        op.create_table(
            "leave_requests",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("employee_id", sa.Integer, sa.ForeignKey("employees.id"),
                      nullable=False, index=True),
            sa.Column("leave_type_id", sa.Integer, sa.ForeignKey("leave_types.id"),
                      nullable=False),
            sa.Column("start_date", sa.Date, nullable=False),
            sa.Column("end_date", sa.Date, nullable=False),
            sa.Column("days_count", sa.Numeric(6, 2), nullable=False, server_default="0"),
            sa.Column("reason", sa.Text),
            sa.Column("status", sa.String(16), nullable=False, server_default="PENDING", index=True),
            sa.Column("reviewed_by", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("reviewed_at", sa.DateTime),
            sa.Column("review_note", sa.Text),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
            sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
        )

    # ─── attendance_exceptions ───────────────────────────────────────────
    if not _has_table("attendance_exceptions"):
        op.create_table(
            "attendance_exceptions",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("employee_id", sa.Integer, sa.ForeignKey("employees.id"),
                      nullable=False, index=True),
            sa.Column("date", sa.Date, nullable=False, index=True),
            sa.Column("type", sa.String(20), nullable=False),
            sa.Column("duration_hours", sa.Numeric(4, 2)),
            sa.Column("note", sa.Text),
            sa.Column("leave_request_id", sa.Integer, sa.ForeignKey("leave_requests.id"), nullable=True),
            sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
            sa.UniqueConstraint("employee_id", "date", name="uq_exception_employee_date"),
        )

    # ─── payroll_lines.attendance_auto_calculated ────────────────────────
    with op.batch_alter_table("payroll_lines") as batch:
        if not _has_column("payroll_lines", "attendance_auto_calculated"):
            batch.add_column(sa.Column("attendance_auto_calculated", sa.Boolean,
                                       nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("payroll_lines") as batch:
        if _has_column("payroll_lines", "attendance_auto_calculated"):
            try:
                batch.drop_column("attendance_auto_calculated")
            except Exception:
                pass
    for table in ("attendance_exceptions", "leave_requests",
                  "leave_balances", "leave_types"):
        if _has_table(table):
            try:
                op.drop_table(table)
            except Exception:
                pass
