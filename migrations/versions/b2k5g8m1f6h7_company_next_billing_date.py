"""MARSOUD-SAAS-DEFERRED-INVOICE-01 (Batch 8 Ticket 2, 2026-07-30).

New nullable Date column: `companies.next_billing_date`. Set at
payment time to the future date on which the next SaaS invoice
should be created. The `process_saas_next_invoices()` cron sweep
picks up companies whose date has arrived, creates the invoice,
and clears the column. NULL = no pending SaaS invoice.

Backfill note: existing tenants whose payment ALREADY created
their next invoice (pre-Batch-8 behaviour) have that invoice
sitting in the DB — no backfill needed. Only NEW payments after
this deploy use the deferred flow.

Revision ID: b2k5g8m1f6h7
Revises: a1j4f7k0e5g6
Create Date: 2026-07-30
"""
from alembic import op
import sqlalchemy as sa


revision = 'b2k5g8m1f6h7'
down_revision = 'a1j4f7k0e5g6'
branch_labels = None
depends_on = None


def _has_col(insp, table, col):
    return any(c["name"] == col for c in insp.get_columns(table))


def upgrade():
    insp = sa.inspect(op.get_bind())
    if not _has_col(insp, "companies", "next_billing_date"):
        with op.batch_alter_table("companies") as batch:
            batch.add_column(sa.Column(
                "next_billing_date", sa.Date(), nullable=True))
        # Index for the cron sweep's `WHERE next_billing_date <=
        # today AND next_billing_date IS NOT NULL` query. Cheap
        # since most rows will be NULL.
        try:
            op.create_index(
                "ix_companies_next_billing_date",
                "companies", ["next_billing_date"])
        except Exception:
            pass


def downgrade():
    insp = sa.inspect(op.get_bind())
    if _has_col(insp, "companies", "next_billing_date"):
        try:
            op.drop_index("ix_companies_next_billing_date",
                          table_name="companies")
        except Exception:
            pass
        with op.batch_alter_table("companies") as batch:
            batch.drop_column("next_billing_date")
