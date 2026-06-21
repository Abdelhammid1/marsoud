"""MARSOUD-COMM-01 Phase A — customer ↔ sales rep + commission ledger

Adds:
  customers.sales_rep_id      FK → users.id nullable
  customers.commission_rate   Numeric(5,2) nullable  (% of pre-tax taxable share)

New table sales_commissions:
  id, company_id, sales_rep_id, customer_id, invoice_id, payment_id,
  taxable_base (Numeric 15,4) — pre-tax portion of the payment
  amount (Numeric 15,4)       — commission earned on this row
  commission_rate (Numeric 5,2) — snapshot of the rate at the moment
  period_year, period_month   — bucket for payroll integration
  status (UNPAID/PAID)
  payroll_run_id              — set when commission is paid via payroll
  is_carry_forward (Boolean)  — reserved for Phase B (negative carry-fwd
                                from refund of an already-paid commission)
  journal_entry_id            — Dr 5280 / Cr 2150 for this row
  created_at

Seeds 2 new accounts (2150 + 5280) for every existing active company
so the commission posting service can find them on day one.

Revision ID: ab_2e7c4a9d5f1
Revises: aa_1f3d6b8e0a7
Create Date: 2026-06-21
"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime

revision = 'ab_2e7c4a9d5f1'
down_revision = 'aa_1f3d6b8e0a7'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


# Accounts to seed per company (code, name, name_ar, type, parent_code,
# normal_side). We seed them inline here so existing companies get them
# without re-running seed_coa.
NEW_ACCOUNTS = [
    ("2150", "Sales Commissions Payable", "عمولات مبيعات مستحقة",
     "LIABILITY", "2100", "CREDIT"),
    ("5280", "Sales Commissions Expense", "مصروف عمولات المبيعات",
     "EXPENSE", "5200", "DEBIT"),
]


def upgrade():
    conn = op.get_bind()

    # ── Customer columns ────────────────────────────────────────────
    if not _has_col("customers", "sales_rep_id"):
        with op.batch_alter_table("customers", schema=None) as batch:
            batch.add_column(sa.Column("sales_rep_id", sa.Integer(),
                                        nullable=True))
            batch.create_foreign_key(
                "fk_customers_sales_rep_id", "users",
                ["sales_rep_id"], ["id"],
            )
    if not _has_col("customers", "commission_rate"):
        with op.batch_alter_table("customers", schema=None) as batch:
            batch.add_column(sa.Column("commission_rate", sa.Numeric(5, 2),
                                        nullable=True))

    # ── sales_commissions table ─────────────────────────────────────
    if not _has_table("sales_commissions"):
        op.create_table(
            "sales_commissions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("sales_rep_id", sa.Integer(),
                      sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("customer_id", sa.Integer(),
                      sa.ForeignKey("customers.id"), nullable=True),
            sa.Column("invoice_id", sa.Integer(),
                      sa.ForeignKey("invoices.id"),
                      nullable=False, index=True),
            sa.Column("payment_id", sa.Integer(),
                      sa.ForeignKey("payments.id"),
                      nullable=True, index=True),
            sa.Column("taxable_base", sa.Numeric(15, 4), nullable=False,
                      server_default="0"),
            sa.Column("amount", sa.Numeric(15, 4), nullable=False,
                      server_default="0"),
            sa.Column("commission_rate", sa.Numeric(5, 2), nullable=False,
                      server_default="0"),
            sa.Column("period_year", sa.Integer(), nullable=False),
            sa.Column("period_month", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(15), nullable=False,
                      server_default="UNPAID", index=True),
            sa.Column("payroll_run_id", sa.Integer(),
                      sa.ForeignKey("payroll_runs.id"), nullable=True),
            sa.Column("is_carry_forward", sa.Boolean(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("journal_entry_id", sa.Integer(),
                      sa.ForeignKey("journal_entries.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True,
                      server_default=sa.func.current_timestamp()),
        )

    # ── Backfill 2 new accounts per existing company ────────────────
    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    companies = conn.execute(sa.text(
        "SELECT id FROM companies WHERE is_active = 1"
    )).fetchall()
    for (cid,) in companies:
        # Find parent ids for "2100" and "5200" in this company
        for code, name, name_ar, atype, parent_code, normal_side in NEW_ACCOUNTS:
            already = conn.execute(sa.text(
                "SELECT id FROM accounts WHERE company_id = :cid AND code = :code"
            ), {"cid": cid, "code": code}).first()
            if already:
                continue
            parent_id = None
            if parent_code:
                p = conn.execute(sa.text(
                    "SELECT id FROM accounts WHERE company_id = :cid AND code = :pc"
                ), {"cid": cid, "pc": parent_code}).first()
                parent_id = p[0] if p else None
            conn.execute(sa.text(
                "INSERT INTO accounts (company_id, code, name, name_ar, "
                "type, normal_side, parent_id, is_active) "
                "VALUES (:cid, :code, :name, :name_ar, :atype, :ns, :pid, 1)"
            ), {"cid": cid, "code": code, "name": name, "name_ar": name_ar,
                "atype": atype, "ns": normal_side, "pid": parent_id})


def downgrade():
    if _has_table("sales_commissions"):
        op.drop_table("sales_commissions")
    if _has_col("customers", "commission_rate"):
        with op.batch_alter_table("customers", schema=None) as batch:
            batch.drop_column("commission_rate")
    if _has_col("customers", "sales_rep_id"):
        with op.batch_alter_table("customers", schema=None) as batch:
            try:
                batch.drop_constraint("fk_customers_sales_rep_id",
                                       type_="foreignkey")
            except Exception:
                pass
            batch.drop_column("sales_rep_id")
    # Account rows left in place — safe to keep (no FK depends on them).
