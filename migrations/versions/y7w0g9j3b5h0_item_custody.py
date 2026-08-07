"""MARSOUD-ITEM-CUSTODY-01 (2026-08-07) — item (physical) custody
tables.

Three tables mirror cash-custody's structure:
  custody_items              — item registry (fixed-asset-linked
                                OR standalone)
  item_custody_requests      — request lifecycle
  item_custodies             — live custody + settlement + disposal
                                bridge

Same CHECK-constraint "exactly one holder" as cash-custody, so
crafted POSTs can't leave both employee_id and department_id set
(or both null).

Chains from asset-disposal (x6v9f8i2a4g9) on this branch. When the
two dep tickets land on main separately, this branch's rebase
will realign the chain automatically (both dep migrations produce
identical patches on main).

Revision ID: y7w0g9j3b5h0
Revises: x6v9f8i2a4g9
Create Date: 2026-08-07
"""
from alembic import op
import sqlalchemy as sa


revision = 'y7w0g9j3b5h0'
down_revision = 'w5u8e7h1z3f8'
branch_labels = None
depends_on = None


ITEMS_TABLE = "custody_items"
REQUESTS_TABLE = "item_custody_requests"
CUSTODIES_TABLE = "item_custodies"

# Same shape as cash-custody's constraint at app/models/cash_custody.py
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


def upgrade():
    # ─── custody_items ────────────────────────────────────────
    if not _has_table(ITEMS_TABLE):
        op.create_table(
            ITEMS_TABLE,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("serial_number", sa.String(100), nullable=True),
            sa.Column("category", sa.String(60), nullable=True),
            sa.Column("fixed_asset_id", sa.Integer,
                      sa.ForeignKey("fixed_assets.id",
                                    ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("estimated_value", sa.Numeric(15, 2),
                      nullable=True),
            sa.Column("is_active", sa.Boolean, nullable=False,
                      server_default=sa.true(), index=True),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.current_timestamp()),
            sa.Column("created_by", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=True),
        )

    # ─── item_custody_requests ────────────────────────────────
    if not _has_table(REQUESTS_TABLE):
        op.create_table(
            REQUESTS_TABLE,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("item_id", sa.Integer,
                      sa.ForeignKey("custody_items.id",
                                    ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("holder_type", sa.String(16),
                      nullable=False, index=True),
            sa.Column("employee_id", sa.Integer,
                      sa.ForeignKey("employees.id",
                                    ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("department_id", sa.Integer,
                      sa.ForeignKey("departments.id",
                                    ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("purpose", sa.Text, nullable=False),
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
            sa.CheckConstraint(
                _HOLDER_CHECK,
                name="ck_item_custody_request_one_holder"),
        )

    # ─── item_custodies ───────────────────────────────────────
    if not _has_table(CUSTODIES_TABLE):
        op.create_table(
            CUSTODIES_TABLE,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                      sa.ForeignKey("companies.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("item_id", sa.Integer,
                      sa.ForeignKey("custody_items.id",
                                    ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("request_id", sa.Integer,
                      sa.ForeignKey("item_custody_requests.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("holder_type", sa.String(16),
                      nullable=False, index=True),
            sa.Column("employee_id", sa.Integer,
                      sa.ForeignKey("employees.id",
                                    ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("department_id", sa.Integer,
                      sa.ForeignKey("departments.id",
                                    ondelete="SET NULL"),
                      nullable=True, index=True),
            sa.Column("handed_over_on", sa.Date, nullable=False),
            sa.Column("condition_at_handover", sa.Text, nullable=True),
            sa.Column("status", sa.String(24), nullable=False,
                      server_default="ACTIVE", index=True),
            sa.Column("settled_on", sa.Date, nullable=True),
            sa.Column("settled_by", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("settlement_note", sa.Text, nullable=True),
            sa.Column("condition_at_return", sa.Text, nullable=True),
            sa.Column("damage_value", sa.Numeric(15, 2),
                      nullable=False, server_default="0"),
            sa.Column("charged_to_employee", sa.Boolean,
                      nullable=False, server_default=sa.false()),
            sa.Column("journal_entry_id", sa.Integer,
                      sa.ForeignKey("journal_entries.id",
                                    ondelete="SET NULL"),
                      nullable=True),
            sa.Column("disposal_pending_at", sa.DateTime,
                      nullable=True, index=True),
            sa.Column("disposal_asset_result_id", sa.Integer,
                      sa.ForeignKey(
                          "fixed_assets.id", ondelete="SET NULL",
                          name="fk_item_custody_disposal_asset"),
                      nullable=True),
            # Self-FK: TRANSFERRED chain link. Use a lambda-style
            # ForeignKey with a use_alter to avoid the self-ref
            # bootstrap ordering issue at CREATE TABLE.
            sa.Column("transferred_to_custody_id", sa.Integer,
                      nullable=True),
            sa.Column("overdue_notified_at", sa.DateTime,
                      nullable=True),
            sa.Column("created_at", sa.DateTime,
                      server_default=sa.func.current_timestamp()),
            sa.Column("created_by", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("cancelled_by", sa.Integer,
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("cancelled_at", sa.DateTime, nullable=True),
            sa.Column("cancel_reason", sa.Text, nullable=True),
            sa.CheckConstraint(
                _HOLDER_CHECK,
                name="ck_item_custody_one_holder"),
        )
        # Add the self-FK after the table exists (SQLite tolerates
        # this fine; other DBs would too via ALTER).
        with op.batch_alter_table(CUSTODIES_TABLE) as bop:
            bop.create_foreign_key(
                "fk_item_custody_transferred_to",
                "item_custodies", ["transferred_to_custody_id"], ["id"],
                ondelete="SET NULL")


def downgrade():
    for tbl in (CUSTODIES_TABLE, REQUESTS_TABLE, ITEMS_TABLE):
        if _has_table(tbl):
            op.drop_table(tbl)
