"""MARSOUD-51 — PDF redesign: bank info + invoice fields + payslip attendance

Adds:
  - companies.bank_name              String(150) nullable
  - companies.bank_account_holder    String(150) nullable
  - companies.bank_account_number    String(50)  nullable
  - companies.iban                   String(50)  nullable

  - invoices.po_reference            String(100) nullable
  - invoices.sales_rep_id            Integer FK → users.id nullable
  - invoices.payment_terms_days      Integer nullable

  - employees.bank_account_last4     String(4)   nullable
  - payroll_lines.absences_count     Integer default 0
  - payroll_lines.late_hours         Numeric(5,2) default 0
  - payroll_lines.overtime_hours     Numeric(5,2) default 0
  - payroll_lines.leaves_count       Integer default 0
  - payroll_lines.payment_method     String(30)  nullable
  - payroll_lines.payment_date       Date        nullable

Revision ID: u9b6e3d2a8f4
Revises: t8a5b2c9e4f7
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = 'u9b6e3d2a8f4'
down_revision = 't8a5b2c9e4f7'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def _add(table, col, type_, **kw):
    if not _has_col(table, col):
        with op.batch_alter_table(table, schema=None) as batch:
            batch.add_column(sa.Column(col, type_, **kw))


def upgrade():
    # Companies — bank info for the invoice PDF
    _add("companies", "bank_name", sa.String(150), nullable=True)
    _add("companies", "bank_account_holder", sa.String(150), nullable=True)
    _add("companies", "bank_account_number", sa.String(50), nullable=True)
    _add("companies", "iban", sa.String(50), nullable=True)

    # Invoices — optional PDF fields
    _add("invoices", "po_reference", sa.String(100), nullable=True)
    _add("invoices", "payment_terms_days", sa.Integer(), nullable=True)
    if not _has_col("invoices", "sales_rep_id"):
        with op.batch_alter_table("invoices", schema=None) as batch:
            batch.add_column(sa.Column("sales_rep_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_invoices_sales_rep_id", "users",
                ["sales_rep_id"], ["id"],
            )

    # Employees — last 4 of bank account for payslip's "paid" box
    _add("employees", "bank_account_last4", sa.String(4), nullable=True)

    # PayrollLines — attendance counts + payment-method info for payslip header
    _add("payroll_lines", "absences_count", sa.Integer(), nullable=True,
         server_default="0")
    _add("payroll_lines", "late_hours", sa.Numeric(5, 2), nullable=True,
         server_default="0")
    _add("payroll_lines", "overtime_hours", sa.Numeric(5, 2), nullable=True,
         server_default="0")
    _add("payroll_lines", "leaves_count", sa.Integer(), nullable=True,
         server_default="0")
    _add("payroll_lines", "payment_method", sa.String(30), nullable=True)
    _add("payroll_lines", "payment_date", sa.Date(), nullable=True)


def downgrade():
    for table, col in [
        ("payroll_lines", "payment_date"),
        ("payroll_lines", "payment_method"),
        ("payroll_lines", "leaves_count"),
        ("payroll_lines", "overtime_hours"),
        ("payroll_lines", "late_hours"),
        ("payroll_lines", "absences_count"),
        ("employees", "bank_account_last4"),
        ("invoices", "payment_terms_days"),
        ("invoices", "po_reference"),
        ("companies", "iban"),
        ("companies", "bank_account_number"),
        ("companies", "bank_account_holder"),
        ("companies", "bank_name"),
    ]:
        if _has_col(table, col):
            with op.batch_alter_table(table, schema=None) as batch:
                batch.drop_column(col)
    if _has_col("invoices", "sales_rep_id"):
        with op.batch_alter_table("invoices", schema=None) as batch:
            try:
                batch.drop_constraint("fk_invoices_sales_rep_id", type_="foreignkey")
            except Exception:
                pass
            batch.drop_column("sales_rep_id")
