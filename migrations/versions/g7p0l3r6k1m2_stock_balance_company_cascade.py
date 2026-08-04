"""MARSOUD-STOCK-BALANCE-CASCADE (2026-08-04).

`stock_balances` kept coming back with orphaned rows — 5 of them, twice,
once on an isolated test environment and once on production during the
employee-advances migration. The boot-time sweep cleaned them and they
returned, which is the signature of a live data-integrity gap rather than
one historical accident.

The gap is in hard_delete_company (app/services/lifecycle.py): it purges a
company by walking every table that HAS a company_id column. Of the
inventory tables only `stock_balances` lacks one — stock_movements and
stock_lots both have it — so the purge deleted product_variants and
warehouses and skipped the balances entirely, orphaning every row on BOTH
foreign keys at once.

Neither safety net caught it:
  · variant_id already declared ondelete="CASCADE", but SQLite does not
    enforce foreign keys unless PRAGMA foreign_keys=ON, which this app
    never sets. In dev the CASCADE is inert. (Postgres does enforce it,
    which is why prod raised IntegrityError instead of silently
    orphaning — the same bug wearing a different face.)
  · warehouse_id had no ondelete at all, and no orphan sweep has ever
    checked the warehouse side, so that half was invisible.

This is the identical bug that migration a6c9f2e5b8d1 fixed for the
invoice children, and the fix is deliberately the same shape:

  1. add company_id, backfilled from the owning product_variant
  2. delete rows that still have none — those ARE the orphans, purged
     here rather than left for the sweep, so a run on prod cleans up
     what past hard_delete_company calls left behind
  3. NOT NULL + index + FK to companies ON DELETE CASCADE
  4. rebuild the warehouse_id FK with ON DELETE CASCADE as a second net

After (1)-(3) the generic loop in hard_delete_company finds the table on
its own, with no change to lifecycle.py — which is the point.

Idempotent and guarded: safe to re-run, and a no-op if the column is
already present.

Revision ID: g7p0l3r6k1m2
Revises: f6o9k2q5j0l1
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa


revision = 'g7p0l3r6k1m2'
down_revision = 'f6o9k2q5j0l1'
branch_labels = None
depends_on = None

TABLE = "stock_balances"


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def _existing_fk_name(table, cols):
    """Name of the FK on `cols`, or None. SQLite FKs are often unnamed,
    hence the tolerant drop below."""
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return None
    for fk in insp.get_foreign_keys(table):
        if fk["constrained_columns"] == list(cols):
            return fk["name"]
    return None


def upgrade():
    if not _has_table(TABLE):
        return
    bind = op.get_bind()

    if not _has_col(TABLE, "company_id"):
        with op.batch_alter_table(TABLE, schema=None) as batch:
            batch.add_column(
                sa.Column("company_id", sa.Integer(), nullable=True))

        # Backfill from the variant, which is the row's real owner.
        bind.execute(sa.text(
            f"UPDATE {TABLE} SET company_id = "
            f"(SELECT product_variants.company_id FROM product_variants "
            f" WHERE product_variants.id = {TABLE}.variant_id) "
            f"WHERE company_id IS NULL"))

        # Anything still NULL has no surviving variant: that is exactly
        # the orphan set the sweep kept reporting. Purge it here so the
        # table is clean before the NOT NULL constraint lands.
        purged = bind.execute(sa.text(
            f"DELETE FROM {TABLE} WHERE company_id IS NULL")).rowcount
        if purged:
            print(f"  [{revision}] purged {purged} orphaned "
                  f"{TABLE} row(s) with no surviving variant")

        # A row can also be orphaned on the warehouse side only — the
        # half no sweep has ever looked at. Same treatment.
        purged_wh = bind.execute(sa.text(
            f"DELETE FROM {TABLE} WHERE warehouse_id NOT IN "
            f"(SELECT id FROM warehouses)")).rowcount
        if purged_wh:
            print(f"  [{revision}] purged {purged_wh} orphaned "
                  f"{TABLE} row(s) with no surviving warehouse")

        with op.batch_alter_table(TABLE, schema=None) as batch:
            batch.alter_column("company_id", nullable=False)
            batch.create_foreign_key(
                f"fk_{TABLE}_company_id",
                "companies", ["company_id"], ["id"],
                ondelete="CASCADE",
            )
            batch.create_index(f"ix_{TABLE}_company_id", ["company_id"])

    # Second net: warehouse_id had no ondelete at all. It must REPLACE
    # the old constraint, not join it — two FKs on the same column means
    # the non-cascading one still blocks the delete on Postgres, which is
    # the whole failure we are removing.
    existing = _existing_fk_name(TABLE, ["warehouse_id"])
    if existing:
        # Postgres (and any named FK): drop by name, then recreate.
        with op.batch_alter_table(TABLE, schema=None) as batch:
            try:
                batch.drop_constraint(existing, type_="foreignkey")
            except Exception:
                pass
            batch.create_foreign_key(
                f"fk_{TABLE}_warehouse_id_cascade",
                "warehouses", ["warehouse_id"], ["id"],
                ondelete="CASCADE",
            )
    else:
        # SQLite: the original FK is unnamed, so drop_constraint has
        # nothing to target and batch mode would faithfully copy it onto
        # the rebuilt table alongside the new one. Reflect the table,
        # strip that constraint, and rebuild from the edited definition.
        reflected = sa.Table(TABLE, sa.MetaData(), autoload_with=bind)
        for c in list(reflected.constraints):
            if (isinstance(c, sa.ForeignKeyConstraint)
                    and [col.name for col in c.columns] == ["warehouse_id"]):
                reflected.constraints.discard(c)
        with op.batch_alter_table(
                TABLE, schema=None, copy_from=reflected) as batch:
            batch.create_foreign_key(
                f"fk_{TABLE}_warehouse_id_cascade",
                "warehouses", ["warehouse_id"], ["id"],
                ondelete="CASCADE",
            )


def downgrade():
    if not _has_table(TABLE):
        return
    with op.batch_alter_table(TABLE, schema=None) as batch:
        try:
            batch.drop_constraint(
                f"fk_{TABLE}_warehouse_id_cascade", type_="foreignkey")
        except Exception:
            pass
        batch.create_foreign_key(
            None, "warehouses", ["warehouse_id"], ["id"])
    if _has_col(TABLE, "company_id"):
        with op.batch_alter_table(TABLE, schema=None) as batch:
            try:
                batch.drop_index(f"ix_{TABLE}_company_id")
            except Exception:
                pass
            try:
                batch.drop_constraint(
                    f"fk_{TABLE}_company_id", type_="foreignkey")
            except Exception:
                pass
            batch.drop_column("company_id")
