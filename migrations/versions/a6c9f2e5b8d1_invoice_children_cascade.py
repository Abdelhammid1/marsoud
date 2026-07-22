"""MARSOUD-POS-ORPHAN-CASCADE (Abdelhamid 2026-07-22).

Two invariants restored on every table that hangs off `invoices`:

  1. `ON DELETE CASCADE` at the DB level on the invoice_id FK. Model-
     level `cascade="all, delete-orphan"` only fires when the delete
     goes through the ORM — bulk SQL (hard_delete_company,
     restore-from-backup, manual DBA delete) bypasses it and leaves
     orphan child rows behind. When the primary key gets reused
     (SQLite always, Postgres after sequence reset / restore), those
     orphans are re-adopted by SQLAlchemy's `.items` / `.payments`
     relationship loader — this is what Abdelhamid observed on invoice
     82 (one-variant `create_pos_order` call returned an invoice with
     two lines and quantity 3).

  2. A `company_id` NOT NULL column on the three child tables that
     don't have one (`invoice_items`, `payments`,
     `invoice_reminders_sent`). Backfilled from the parent invoice.
     Two goals: (a) makes future company-scoped bulk-deletes complete
     without needing the invoice join; (b) enables cheap zombie
     sweeps that don't have to walk from `invoices` down.

Six child tables total: invoice_items, payments, invoice_reminders_sent,
credit_notes, refunds, sales_commissions. The three without
`company_id` also get the column; the other three just get CASCADE.

Idempotent + reversible (down() restores nullable + drops the CASCADE).

Revision ID: a6c9f2e5b8d1
Revises: z5b8e4d9c2a6
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'a6c9f2e5b8d1'
down_revision = 'z5b8e4d9c2a6'
branch_labels = None
depends_on = None


CHILD_TABLES_MISSING_COMPANY_ID = (
    "invoice_items",
    "payments",
    "invoice_reminders_sent",
)
CHILD_TABLES_WITH_COMPANY_ID = (
    "credit_notes",
    "refunds",
    "sales_commissions",
)
ALL_CHILDREN = CHILD_TABLES_MISSING_COMPANY_ID + CHILD_TABLES_WITH_COMPANY_ID


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def _existing_fk_name(table, cols):
    """Return the name of the FK on `cols` if present (may be None
    for unnamed FKs on SQLite). Used so we can drop-and-recreate."""
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return None
    for fk in insp.get_foreign_keys(table):
        if fk["constrained_columns"] == list(cols):
            return fk["name"]
    return None


def upgrade():
    bind = op.get_bind()

    # ── Part 1: add company_id where missing, backfilled from invoice ──
    for tbl in CHILD_TABLES_MISSING_COMPANY_ID:
        if _has_col(tbl, "company_id"):
            continue
        with op.batch_alter_table(tbl, schema=None) as batch:
            batch.add_column(
                sa.Column("company_id", sa.Integer(), nullable=True))
        # Backfill from the parent invoice.
        bind.execute(sa.text(
            f"UPDATE {tbl} SET company_id = "
            f"(SELECT invoices.company_id FROM invoices "
            f" WHERE invoices.id = {tbl}.invoice_id) "
            f"WHERE company_id IS NULL"))
        # Purge any orphan rows (invoice_id points at a deleted parent).
        # Not silently — this is the zombie-sweep half of the fix and we
        # deliberately do it inside the migration so a rerun on prod
        # cleans the mess left behind by past hard_delete_company runs.
        bind.execute(sa.text(
            f"DELETE FROM {tbl} WHERE company_id IS NULL"))
        # NOT NULL + index + FK. Composite index on (company_id, invoice_id)
        # is cheap and speeds up the common company-scoped queries.
        with op.batch_alter_table(tbl, schema=None) as batch:
            batch.alter_column("company_id", nullable=False)
            batch.create_foreign_key(
                f"fk_{tbl}_company_id",
                "companies", ["company_id"], ["id"],
                ondelete="CASCADE",
            )
            batch.create_index(
                f"ix_{tbl}_company_id", ["company_id"])

    # ── Part 2: recreate invoice_id FK with ON DELETE CASCADE ──
    # batch_alter_table on SQLite recreates the table under the hood,
    # so we drop the existing FK (by name if we know it, else fall
    # back to the batch's implicit rebuild) and recreate.
    for tbl in ALL_CHILDREN:
        insp = sa.inspect(bind)
        if tbl not in insp.get_table_names():
            continue
        existing = _existing_fk_name(tbl, ["invoice_id"])
        with op.batch_alter_table(tbl, schema=None) as batch:
            if existing:
                try:
                    batch.drop_constraint(existing, type_="foreignkey")
                except Exception:
                    # SQLite's batch mode sometimes drops FKs implicitly
                    # during the table rebuild — a redundant drop then
                    # errors. Ignore and let the recreate stand.
                    pass
            batch.create_foreign_key(
                f"fk_{tbl}_invoice_id_cascade",
                "invoices", ["invoice_id"], ["id"],
                ondelete="CASCADE",
            )


def downgrade():
    bind = op.get_bind()
    # Drop the added CASCADE FKs, restore un-cascaded FKs.
    for tbl in ALL_CHILDREN:
        insp = sa.inspect(bind)
        if tbl not in insp.get_table_names():
            continue
        with op.batch_alter_table(tbl, schema=None) as batch:
            try:
                batch.drop_constraint(
                    f"fk_{tbl}_invoice_id_cascade", type_="foreignkey")
            except Exception:
                pass
            batch.create_foreign_key(
                None, "invoices", ["invoice_id"], ["id"])
    # Drop company_id from the three tables that gained it. NOT NULL is
    # lifted first so a partial rollback doesn't error on inserts.
    for tbl in CHILD_TABLES_MISSING_COMPANY_ID:
        if not _has_col(tbl, "company_id"):
            continue
        with op.batch_alter_table(tbl, schema=None) as batch:
            try:
                batch.drop_constraint(
                    f"fk_{tbl}_company_id", type_="foreignkey")
            except Exception:
                pass
            try:
                batch.drop_index(f"ix_{tbl}_company_id")
            except Exception:
                pass
            batch.drop_column("company_id")
