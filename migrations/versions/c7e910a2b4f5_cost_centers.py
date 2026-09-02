"""MARSOUD-COST-CENTERS-01 — cost_centers table + cost_center_id
columns on journal_lines, vendor_bill_items, invoice_items.

Revision ID: c7e910a2b4f5
Revises: b3d5a8f1e720
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "c7e910a2b4f5"
down_revision = "b3d5a8f1e720"
branch_labels = None
depends_on = None

TABLE = "cost_centers"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_col(table, col):
    if not _has_table(table):
        return False
    return col in {c["name"] for c in _inspector().get_columns(table)}


def upgrade():
    if not _has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                        sa.ForeignKey("companies.id"),
                        nullable=False, index=True),
            sa.Column("code", sa.String(20), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("name_ar", sa.String(120), nullable=True),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("is_active", sa.Boolean,
                        server_default=sa.true(), nullable=False),
            sa.Column("linked_department_id", sa.Integer,
                        sa.ForeignKey("departments.id",
                                        name="fk_cost_center_department"),
                        nullable=True),
            sa.Column("created_at", sa.DateTime,
                        server_default=sa.func.current_timestamp(),
                        nullable=False),
            sa.Column("deleted_at", sa.DateTime, nullable=True),
            sa.UniqueConstraint("company_id", "code",
                                 name="uq_cost_center_company_code"),
        )

    # ─ journal_lines.cost_center_id ──────────────────────────
    if _has_table("journal_lines") and not _has_col(
            "journal_lines", "cost_center_id"):
        with op.batch_alter_table("journal_lines") as batch:
            batch.add_column(sa.Column(
                "cost_center_id", sa.Integer,
                sa.ForeignKey("cost_centers.id",
                                name="fk_journal_line_cost_center"),
                nullable=True, index=True))

    # ─ vendor_bill_items.cost_center_id ─────────────────────
    if _has_table("vendor_bill_items") and not _has_col(
            "vendor_bill_items", "cost_center_id"):
        with op.batch_alter_table("vendor_bill_items") as batch:
            batch.add_column(sa.Column(
                "cost_center_id", sa.Integer,
                sa.ForeignKey("cost_centers.id",
                                name="fk_vbill_item_cost_center"),
                nullable=True, index=True))

    # ─ invoice_items.cost_center_id ─────────────────────────
    if _has_table("invoice_items") and not _has_col(
            "invoice_items", "cost_center_id"):
        with op.batch_alter_table("invoice_items") as batch:
            batch.add_column(sa.Column(
                "cost_center_id", sa.Integer,
                sa.ForeignKey("cost_centers.id",
                                name="fk_invoice_item_cost_center"),
                nullable=True, index=True))


def downgrade():
    for table in ("invoice_items", "vendor_bill_items", "journal_lines"):
        if _has_table(table) and _has_col(table, "cost_center_id"):
            with op.batch_alter_table(table) as batch:
                batch.drop_column("cost_center_id")
    if _has_table(TABLE):
        op.drop_table(TABLE)
