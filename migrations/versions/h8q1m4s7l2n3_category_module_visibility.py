"""MARSOUD-CATEGORY-VISIBILITY-01 (2026-08-04).

A product used to appear in every operational module or none. Raw
materials bought for manufacturing — cloth, thread — showed up on the POS
cashier screen, where they have no business being: nobody sells them to a
walk-in customer, they get consumed inside a work order.

Visibility is now decided per CATEGORY, which is the level that matches
how people actually organise this (raw materials tend to live in one
category, so it is one decision rather than one per product):

    visible_in_pos                نقطة البيع
    visible_in_manufacturing      التصنيع
    visible_in_vendor_bills       فواتير الموردين
    visible_in_customer_invoices  فواتير العملاء

All four land as NOT NULL with server_default TRUE, so every category that
already exists stays visible in all four places and nothing disappears on
deploy. That is an explicit acceptance criterion of the ticket, and it is
also why the filter helper treats "nothing is hidden" as a no-op rather
than as an empty allow-list — with these defaults the queries it guards
run exactly as they did before.

server_default=sa.true() matches how this same table's `is_active` column
was created in d4_f5a8b3c9e2d_product_hierarchy.

Idempotent and guarded: safe to re-run, no-op if the columns are present.

Revision ID: h8q1m4s7l2n3
Revises: g7p0l3r6k1m2
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa


revision = 'h8q1m4s7l2n3'
down_revision = 'g7p0l3r6k1m2'
branch_labels = None
depends_on = None

TABLE = "product_categories"
COLUMNS = (
    "visible_in_pos",
    "visible_in_manufacturing",
    "visible_in_vendor_bills",
    "visible_in_customer_invoices",
)


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_table(TABLE):
        return
    with op.batch_alter_table(TABLE, schema=None) as batch:
        for col in COLUMNS:
            if not _has_col(TABLE, col):
                batch.add_column(sa.Column(
                    col, sa.Boolean(), nullable=False,
                    server_default=sa.true()))

    # Belt and braces: if an earlier partial run left the column present
    # but nullable with NULLs in it, a NULL would read as "hidden" in the
    # helper's flag check. Force every existing row to visible.
    bind = op.get_bind()
    for col in COLUMNS:
        if _has_col(TABLE, col):
            bind.execute(sa.text(
                f"UPDATE {TABLE} SET {col} = 1 WHERE {col} IS NULL"))


def downgrade():
    if not _has_table(TABLE):
        return
    with op.batch_alter_table(TABLE, schema=None) as batch:
        for col in reversed(COLUMNS):
            if _has_col(TABLE, col):
                batch.drop_column(col)
