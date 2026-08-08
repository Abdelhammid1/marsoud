"""MARSOUD-OPS-HUB-EXPANSION-01 (2026-08-08) — payroll GOSI/tax
columns + two new CoA rows (5960 فروق نقدية / 5970 تسويات متنوعة)
backfilled for existing companies.

Adds six new nullable columns:
  employees: insurance_rate, income_tax_rate, company_insurance_rate
  payroll_lines: insurance_deduction, income_tax_deduction,
                 employer_insurance_share

Adds two new CoA leaves under parent 5900 to every existing
company that has the parent — new companies get them via the seed
in `app/services/seed_coa.py`.

All changes additive. Idempotent — reruns are safe via
_has_column / _account_exists guards. Mirror of the same shape used
by x6v9f8i2a4g9_asset_disposal.

Revision ID: a2b5c9d3f6e0
Revises: y7w0g9j3b5h0
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa


revision = "a2b5c9d3f6e0"
down_revision = "y7w0g9j3b5h0"
branch_labels = None
depends_on = None


# ─── idempotency helpers ────────────────────────────────────────────
def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_column(table, col):
    if not _has_table(table):
        return False
    return col in {c["name"] for c in _inspector().get_columns(table)}


def _account_exists(bind, company_id, code):
    row = bind.execute(sa.text(
        "SELECT id FROM accounts WHERE company_id=:c AND code=:k"),
        {"c": company_id, "k": code}).fetchone()
    return row is not None


def _get_account_id(bind, company_id, code):
    row = bind.execute(sa.text(
        "SELECT id FROM accounts WHERE company_id=:c AND code=:k"),
        {"c": company_id, "k": code}).fetchone()
    return row[0] if row else None


def _insert_account(bind, *, company_id, code, name, name_ar,
                     type_, parent_id, is_postable, normal_side):
    bind.execute(sa.text(
        "INSERT INTO accounts "
        "(company_id, code, name, name_ar, type, parent_id, "
        " is_postable, normal_side, is_active) "
        "VALUES (:c, :k, :n, :na, :t, :p, :ip, :ns, 1)"),
        {"c": company_id, "k": code, "n": name, "na": name_ar,
         "t": type_, "p": parent_id, "ip": is_postable, "ns": normal_side})


def upgrade():
    # ── payroll columns ──────────────────────────────────────────
    if _has_table("employees"):
        with op.batch_alter_table("employees") as batch:
            if not _has_column("employees", "insurance_rate"):
                batch.add_column(sa.Column(
                    "insurance_rate", sa.Numeric(15, 4),
                    nullable=True, server_default="0"))
            if not _has_column("employees", "income_tax_rate"):
                batch.add_column(sa.Column(
                    "income_tax_rate", sa.Numeric(15, 4),
                    nullable=True, server_default="0"))
            if not _has_column("employees", "company_insurance_rate"):
                batch.add_column(sa.Column(
                    "company_insurance_rate", sa.Numeric(15, 4),
                    nullable=True, server_default="0"))

    if _has_table("payroll_lines"):
        with op.batch_alter_table("payroll_lines") as batch:
            if not _has_column("payroll_lines", "insurance_deduction"):
                batch.add_column(sa.Column(
                    "insurance_deduction", sa.Numeric(15, 2),
                    nullable=True, server_default="0"))
            if not _has_column("payroll_lines", "income_tax_deduction"):
                batch.add_column(sa.Column(
                    "income_tax_deduction", sa.Numeric(15, 2),
                    nullable=True, server_default="0"))
            if not _has_column("payroll_lines", "employer_insurance_share"):
                batch.add_column(sa.Column(
                    "employer_insurance_share", sa.Numeric(15, 2),
                    nullable=True, server_default="0"))

    # ── CoA backfill for existing tenants ────────────────────────
    # Two new accounts under 5900 (مصروفات أخرى).
    #   5960 — فروق نقدية (cash count adjustments)
    #   5970 — تسويات متنوعة (general adjustments)
    # New tenants get them via seed_default_coa. Skip any tenant
    # whose 5900 parent isn't there — they're either mid-migration
    # or on an ancient COA.
    bind = op.get_bind()
    if not _has_table("accounts") or not _has_table("companies"):
        return
    company_ids = [
        r[0] for r in bind.execute(sa.text(
            "SELECT id FROM companies")).fetchall()
    ]
    for cid in company_ids:
        parent_id = _get_account_id(bind, cid, "5900")
        if parent_id is None:
            continue
        if not _account_exists(bind, cid, "5960"):
            _insert_account(
                bind, company_id=cid, code="5960",
                name="Cash Count Variance",
                name_ar="فروق نقدية",
                type_="EXPENSE", parent_id=parent_id,
                is_postable=True, normal_side="DEBIT")
        if not _account_exists(bind, cid, "5970"):
            _insert_account(
                bind, company_id=cid, code="5970",
                name="Miscellaneous Adjustments",
                name_ar="تسويات متنوعة",
                type_="EXPENSE", parent_id=parent_id,
                is_postable=True, normal_side="DEBIT")


def downgrade():
    # Symmetric column drops; leave the two new CoA rows in place —
    # data (a company's chart of accounts), not schema. Same
    # convention x6v9f8i2a4g9_asset_disposal follows.
    if _has_table("payroll_lines"):
        with op.batch_alter_table("payroll_lines") as batch:
            if _has_column("payroll_lines", "employer_insurance_share"):
                batch.drop_column("employer_insurance_share")
            if _has_column("payroll_lines", "income_tax_deduction"):
                batch.drop_column("income_tax_deduction")
            if _has_column("payroll_lines", "insurance_deduction"):
                batch.drop_column("insurance_deduction")
    if _has_table("employees"):
        with op.batch_alter_table("employees") as batch:
            if _has_column("employees", "company_insurance_rate"):
                batch.drop_column("company_insurance_rate")
            if _has_column("employees", "income_tax_rate"):
                batch.drop_column("income_tax_rate")
            if _has_column("employees", "insurance_rate"):
                batch.drop_column("insurance_rate")
