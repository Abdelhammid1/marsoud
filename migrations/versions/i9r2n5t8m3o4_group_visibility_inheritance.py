"""MARSOUD-CATEGORY-VISIBILITY-01 pt 2 (2026-08-04).

h8q1m4s7l2n3 put four visibility switches on each product CATEGORY. Review
asked for them one level up:

    "انا كنت عاوز الكلام ده يظهر ع اسم المجموعة وبالتالي يتورث للفئات
     اللي تحته، الا لو انا دخلت على فئة وعملت اوفر رايد"

So the decision belongs to the GROUP, every category under it inherits, and
a category may override — per module, not all-or-nothing. That suits the
ticket's own example: "مواد خام" is usually a whole group, and setting it
once should cover every category beneath it.

Shape after this migration:

    product_groups.visible_in_*       NOT NULL, default TRUE
                                      (inheritance bottoms out here, so it
                                       always holds a real answer)

    product_categories.visible_in_*   NULLABLE
                                      NULL  → inherit from my group
                                      TRUE  → override: show
                                      FALSE → override: hide

Effective value is COALESCE(category, group), resolved in exactly one place
— app/services/category_visibility.py.

The data conversion is the part that matters. An existing category holding
TRUE is not expressing an opinion; it is simply the default from pt 2's
migration, so it becomes NULL and starts inheriting. A category holding
FALSE was switched off by hand, so it stays FALSE as an explicit override.
Since every group lands on TRUE, an inheriting category resolves to visible
— which preserves the original ticket's "nothing disappears after deploy"
guarantee straight through this change.

Idempotent and guarded; safe to re-run.

Revision ID: i9r2n5t8m3o4
Revises: h8q1m4s7l2n3
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa


revision = 'i9r2n5t8m3o4'
down_revision = 'h8q1m4s7l2n3'
branch_labels = None
depends_on = None

GROUPS = "product_groups"
CATEGORIES = "product_categories"
COLUMNS = (
    "visible_in_pos",
    "visible_in_manufacturing",
    "visible_in_vendor_bills",
    "visible_in_customer_invoices",
)


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def _col(table, name):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return None
    for c in insp.get_columns(table):
        if c["name"] == name:
            return c
    return None


def _drop_stale_batch_temp(table):
    """Remove the temp table a crashed batch_alter_table leaves behind.

    SQLite has no ALTER COLUMN, so alembic's batch mode copies the table
    into `_alembic_tmp_<name>`, rewrites, and swaps. If the migration dies
    between those steps the temp table survives, and every retry then fails
    with "table _alembic_tmp_x already exists" — the real error buried under
    a misleading one. This migration claims to be re-runnable, so it has to
    clear its own wreckage first.
    """
    if _has_table(f"_alembic_tmp_{table}"):
        op.execute(f"DROP TABLE _alembic_tmp_{table}")


def upgrade():
    if not (_has_table(GROUPS) and _has_table(CATEGORIES)):
        return
    bind = op.get_bind()
    for _t in (GROUPS, CATEGORIES):
        _drop_stale_batch_temp(_t)

    # ── 1. the group gets the real, always-present answer ──
    with op.batch_alter_table(GROUPS, schema=None) as batch:
        for name in COLUMNS:
            if _col(GROUPS, name) is None:
                batch.add_column(sa.Column(
                    name, sa.Boolean(), nullable=False,
                    server_default=sa.true()))
    for name in COLUMNS:
        if _col(GROUPS, name) is not None:
            bind.execute(sa.text(
                f"UPDATE {GROUPS} SET {name} = 1 WHERE {name} IS NULL"))

    # ── 2. the category becomes a tri-state ──
    # Relax NOT NULL FIRST, then convert. The other order looks tidier but
    # cannot work: writing NULL into a still-NOT NULL column is exactly the
    # constraint violation you would expect, and it aborts the migration
    # half-applied.
    with op.batch_alter_table(CATEGORIES, schema=None) as batch:
        for name in COLUMNS:
            existing = _col(CATEGORIES, name)
            if existing is None:
                batch.add_column(sa.Column(name, sa.Boolean(), nullable=True))
            elif not existing["nullable"]:
                batch.alter_column(name, existing_type=sa.Boolean(),
                                    nullable=True, server_default=None)

    for name in COLUMNS:
        if _col(CATEGORIES, name) is not None:
            # TRUE was pt 2's default, not a decision → start inheriting.
            # FALSE was switched off by hand → keep as an explicit override.
            bind.execute(sa.text(
                f"UPDATE {CATEGORIES} SET {name} = NULL WHERE {name} = 1"))


def downgrade():
    if not (_has_table(GROUPS) and _has_table(CATEGORIES)):
        return
    bind = op.get_bind()
    # An inheriting category has to be given a concrete value again; take
    # the one it was resolving to via its group.
    bind.execute(sa.text(
        f"UPDATE {CATEGORIES} SET " + ", ".join(
            f"{n} = COALESCE({n}, (SELECT g.{n} FROM {GROUPS} g "
            f"WHERE g.id = {CATEGORIES}.group_id), 1)" for n in COLUMNS)))
    with op.batch_alter_table(CATEGORIES, schema=None) as batch:
        for name in COLUMNS:
            if _col(CATEGORIES, name) is not None:
                batch.alter_column(name, existing_type=sa.Boolean(),
                                    nullable=False,
                                    server_default=sa.true())
    with op.batch_alter_table(GROUPS, schema=None) as batch:
        for name in reversed(COLUMNS):
            if _col(GROUPS, name) is not None:
                batch.drop_column(name)
