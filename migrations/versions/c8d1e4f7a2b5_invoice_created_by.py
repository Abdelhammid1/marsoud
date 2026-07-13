"""MARSOUD-INVOICE-CREATOR (Abdelhamid 2026-07-13).

Add invoices.created_by_id so the detail page can show "who created this
invoice + when". The `created_at` column already exists; only the
FK to users was missing.

For legacy POS invoices we backfill created_by_id = cashier_id
(the cashier IS the creator for a POS transaction — there's no
separate "who rang it up" concept). Manual invoices with no
recorded creator stay NULL and render as "غير معروف" in the UI.

Revision ID: c8d1e4f7a2b5
Revises: b7c0d3e6f9a2
Create Date: 2026-07-13
"""
from alembic import op
import sqlalchemy as sa


revision = 'c8d1e4f7a2b5'
down_revision = 'b7c0d3e6f9a2'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("invoices", "created_by_id"):
        # SQLite (via batch_alter_table) rejects anonymous FKs, so we
        # add the column first, then the named FK constraint.
        with op.batch_alter_table("invoices", schema=None) as batch:
            batch.add_column(sa.Column(
                "created_by_id", sa.Integer(), nullable=True,
            ))
            batch.create_foreign_key(
                "fk_invoices_created_by_id_users",
                "users", ["created_by_id"], ["id"],
            )

    # Backfill POS invoices whose cashier is the creator by definition.
    # Manual invoices stay NULL — no reliable source for the historical
    # creator id.
    op.execute(sa.text("""
        UPDATE invoices
        SET created_by_id = cashier_id
        WHERE created_by_id IS NULL
          AND cashier_id IS NOT NULL
    """))


def downgrade():
    if _has_col("invoices", "created_by_id"):
        with op.batch_alter_table("invoices", schema=None) as batch:
            batch.drop_column("created_by_id")
