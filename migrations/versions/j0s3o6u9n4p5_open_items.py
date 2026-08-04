"""MARSOUD-OPS-FOUNDATION (2026-08-05) — the generalised open item.

Two-sided operations (accrue then pay, prepay then consume, declare then
disburse, borrow then repay) need a shared notion of "an amount still
owed". Without it every settlement wizard would be a free amount box: the
same accrual could be paid twice, or paid beyond its value, with nothing
to notice.

The shape already exists four times — EmployeeAdvance, EmployeeAccrual,
CustomerDeposit, InvoiceInstallment — so this generalises rather than
invents. Two corrections taken from them:

  · settlements are CHILD ROWS. EmployeeAccrual keeps one
    settlement_journal_entry_id that each partial payment overwrites, so
    earlier legs become untraceable.
  · nothing closes an item that still has a remainder.
    CustomerDeposit.apply_to_invoice marks a partly-used deposit APPLIED
    and silently loses the rest.

Additive: two new tables, nothing existing is touched.

Revision ID: j0s3o6u9n4p5
Revises: i9r2n5t8m3o4
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa


revision = 'j0s3o6u9n4p5'
down_revision = 'i9r2n5t8m3o4'
branch_labels = None
depends_on = None

ITEMS = "open_items"
LEGS = "open_item_settlements"

STATUS = sa.Enum("OPEN", "PARTIALLY_SETTLED", "SETTLED", "CANCELLED",
                 "WRITTEN_OFF", name="openitemstatus")


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade():
    if not _has_table(ITEMS):
        op.create_table(
            ITEMS,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("kind", sa.String(40), nullable=False, index=True),
            sa.Column("description", sa.String(255)),
            sa.Column("account_id", sa.Integer,
                      sa.ForeignKey("accounts.id"), nullable=False),
            sa.Column("party_type", sa.String(20)),
            sa.Column("party_id", sa.Integer),
            sa.Column("original_amount", sa.Numeric(15, 2), nullable=False),
            sa.Column("settled_amount", sa.Numeric(15, 2), nullable=False,
                      server_default="0"),
            sa.Column("status", STATUS, nullable=False,
                      server_default="OPEN", index=True),
            sa.Column("due_date", sa.Date),
            sa.Column("journal_entry_id", sa.Integer,
                      sa.ForeignKey("journal_entries.id")),
            sa.Column("reversal_entry_id", sa.Integer,
                      sa.ForeignKey("journal_entries.id")),
            sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime),
            sa.Column("closed_at", sa.DateTime),
            sa.Column("note", sa.Text),
        )

    if not _has_table(LEGS):
        op.create_table(
            LEGS,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("open_item_id", sa.Integer,
                      sa.ForeignKey("open_items.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("amount", sa.Numeric(15, 2), nullable=False),
            sa.Column("settled_on", sa.Date),
            sa.Column("journal_entry_id", sa.Integer,
                      sa.ForeignKey("journal_entries.id")),
            sa.Column("reversed_at", sa.DateTime),
            sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("created_at", sa.DateTime),
        )


def downgrade():
    if _has_table(LEGS):
        op.drop_table(LEGS)
    if _has_table(ITEMS):
        op.drop_table(ITEMS)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        STATUS.drop(bind, checkfirst=True)
