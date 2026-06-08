"""HR Phase 1 (departments + employee extensions) + per-company logo (Cycle 5)

Revision ID: c7a3f1e0b257
Revises: b5f2e3a91143
Create Date: 2026-06-08 10:00:00

Adds:
  - departments (id, company_id, name, description, manager_employee_id, is_active, created_at)
  - employees.department_id        (HR-01)
  - employees.national_id          (HR-02)
  - employees.nationality          (HR-02)
  - employees.date_of_birth        (HR-02)
  - employees.gender               (HR-02)
  - employees.manager_id           (HR-02, self-FK)
  - employees.contract_end_date    (HR-02, indexed — used by HR-03 cron)
  - employees.contract_alert_last_sent (HR-03, dedup state)
  - employees.notes                (HR-02)
  - companies.logo_path            (MARSOUD-23 — uploaded logo on disk)

All changes are additive and idempotent (each guarded by an inspector probe).
"""
from alembic import op
import sqlalchemy as sa


revision = "c7a3f1e0b257"
down_revision = "b5f2e3a91143"
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


def _has_index(table, index):
    if not _has_table(table):
        return False
    return any(i["name"] == index for i in _inspector().get_indexes(table))


def upgrade():
    # ─── departments ─────────────────────────────────────────────────────
    if not _has_table("departments"):
        op.create_table(
            "departments",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer, sa.ForeignKey("companies.id"), nullable=False, index=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("description", sa.Text),
            sa.Column("manager_employee_id", sa.Integer, sa.ForeignKey("employees.id"), nullable=True),
            sa.Column("is_active", sa.Boolean, server_default=sa.true(), nullable=False),
            sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
            sa.UniqueConstraint("company_id", "name", name="uq_department_company_name"),
        )

    # ─── employees: new columns ──────────────────────────────────────────
    # NOTE: SQLite batch mode requires named constraints. We add FK columns
    # as plain integers (SQLite doesn't enforce FKs by default anyway, and
    # the ORM models still declare the relationship) to avoid the unnamed-
    # constraint error from alembic batch.
    with op.batch_alter_table("employees") as batch:
        if not _has_column("employees", "department_id"):
            batch.add_column(sa.Column("department_id", sa.Integer, nullable=True))
        if not _has_column("employees", "national_id"):
            batch.add_column(sa.Column("national_id", sa.String(50)))
        if not _has_column("employees", "nationality"):
            batch.add_column(sa.Column("nationality", sa.String(60)))
        if not _has_column("employees", "date_of_birth"):
            batch.add_column(sa.Column("date_of_birth", sa.Date))
        if not _has_column("employees", "gender"):
            batch.add_column(sa.Column("gender", sa.String(10)))
        if not _has_column("employees", "manager_id"):
            batch.add_column(sa.Column("manager_id", sa.Integer, nullable=True))
        if not _has_column("employees", "contract_end_date"):
            batch.add_column(sa.Column("contract_end_date", sa.Date))
        if not _has_column("employees", "contract_alert_last_sent"):
            batch.add_column(sa.Column("contract_alert_last_sent", sa.Date))
        if not _has_column("employees", "notes"):
            batch.add_column(sa.Column("notes", sa.Text))

    if _has_column("employees", "department_id") and not _has_index("employees", "ix_employees_department_id"):
        try:
            op.create_index("ix_employees_department_id", "employees", ["department_id"])
        except Exception:
            pass
    if _has_column("employees", "contract_end_date") and not _has_index("employees", "ix_employees_contract_end_date"):
        try:
            op.create_index("ix_employees_contract_end_date", "employees", ["contract_end_date"])
        except Exception:
            pass

    # ─── companies: logo_path (separate from legacy logo_url) ────────────
    with op.batch_alter_table("companies") as batch:
        if not _has_column("companies", "logo_path"):
            batch.add_column(sa.Column("logo_path", sa.String(300)))


def downgrade():
    # Best-effort: drop new bits if they exist. Keep companies.logo_path
    # untouched on downgrade to avoid losing uploaded files.
    with op.batch_alter_table("employees") as batch:
        for col in (
            "notes", "contract_alert_last_sent", "contract_end_date",
            "manager_id", "gender", "date_of_birth", "nationality",
            "national_id", "department_id",
        ):
            if _has_column("employees", col):
                try:
                    batch.drop_column(col)
                except Exception:
                    pass
    if _has_table("departments"):
        try:
            op.drop_table("departments")
        except Exception:
            pass
