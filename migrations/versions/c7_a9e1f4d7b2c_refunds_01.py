"""MARSOUD-REFUNDS-01 — unified sales + purchase refunds.

Adds:
  - refunds.company_id + refunds.number       (backfill from invoice)
  - vendor_bill_refunds table                 (purchase refund audit trail)
  - debit_notes table                         (analog of credit_notes on
                                               the purchase side)
  - New account 5105 "Purchase Returns & Allowances" seeded on every
    existing company that has 5100 (via a data migration in-line).

Revision ID: c7_a9e1f4d7b2c
Revises: c6_e8a4b2f7c3d
"""
from alembic import op
import sqlalchemy as sa


revision = "c7_a9e1f4d7b2c"
down_revision = "c6_e8a4b2f7c3d"
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    # 1. Extend refunds with company_id + number.
    if not _has_col("refunds", "company_id"):
        with op.batch_alter_table("refunds") as batch:
            batch.add_column(sa.Column(
                "company_id", sa.Integer(),
                sa.ForeignKey("companies.id",
                              name="fk_refunds_company_id_companies"),
                nullable=True,
            ))
    if not _has_col("refunds", "number"):
        with op.batch_alter_table("refunds") as batch:
            batch.add_column(sa.Column("number", sa.String(30), nullable=True))
    # Backfill company_id from linked invoice
    op.execute("""
      UPDATE refunds
      SET company_id = (SELECT company_id FROM invoices
                         WHERE invoices.id = refunds.invoice_id)
      WHERE company_id IS NULL
    """)

    # 2. New table: vendor_bill_refunds
    if not _has_table("vendor_bill_refunds"):
        op.create_table(
            "vendor_bill_refunds",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("number", sa.String(30), nullable=True, index=True),
            sa.Column("bill_id", sa.Integer(),
                      sa.ForeignKey("vendor_bills.id"),
                      nullable=False, index=True),
            sa.Column("type", sa.String(20), nullable=False),
            sa.Column("amount", sa.Numeric(15, 4), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("journal_entry_id", sa.Integer(),
                      sa.ForeignKey("journal_entries.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                      server_default=sa.func.current_timestamp()),
        )

    # 3. New table: debit_notes (purchase-side analog of credit_notes)
    if not _has_table("debit_notes"):
        op.create_table(
            "debit_notes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("vendor_id", sa.Integer(),
                      sa.ForeignKey("vendors.id"), nullable=False),
            sa.Column("bill_id", sa.Integer(),
                      sa.ForeignKey("vendor_bills.id"), nullable=True),
            sa.Column("amount", sa.Numeric(15, 4), nullable=False),
            sa.Column("used_amount", sa.Numeric(15, 4),
                      server_default="0"),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                      server_default=sa.func.current_timestamp()),
        )

    # 4. Seed 5105 for every existing company that has 5100.
    #    Runs inline as a data migration; idempotent (skips companies
    #    that already have 5105).
    bind = op.get_bind()
    companies = bind.execute(sa.text(
        "SELECT DISTINCT company_id FROM accounts WHERE code = '5100'"
    )).fetchall()
    for row in companies:
        cid = row[0]
        already = bind.execute(sa.text(
            "SELECT id FROM accounts WHERE company_id = :c AND code = '5105'"
        ), {"c": cid}).fetchone()
        if already:
            continue
        # Get parent 5100 id + type + normal_side to inherit
        parent = bind.execute(sa.text(
            "SELECT id, type, normal_side FROM accounts "
            "WHERE company_id = :c AND code = '5100'"
        ), {"c": cid}).fetchone()
        if not parent:
            continue
        bind.execute(sa.text("""
          INSERT INTO accounts (
            company_id, code, name, name_ar, type, normal_side,
            parent_id, is_active, is_postable
          ) VALUES (
            :c, '5105', 'Purchase Returns & Allowances',
            'مردودات ومسموحات المشتريات', :t, :ns, :pid, 1, 1
          )
        """), {"c": cid, "t": parent[1], "ns": parent[2], "pid": parent[0]})


def downgrade():
    if _has_table("debit_notes"):
        op.drop_table("debit_notes")
    if _has_table("vendor_bill_refunds"):
        op.drop_table("vendor_bill_refunds")
    if _has_col("refunds", "number"):
        with op.batch_alter_table("refunds") as batch:
            batch.drop_column("number")
    if _has_col("refunds", "company_id"):
        with op.batch_alter_table("refunds") as batch:
            batch.drop_column("company_id")
    # 5105 deletion left up to the operator — it can have journal lines.
