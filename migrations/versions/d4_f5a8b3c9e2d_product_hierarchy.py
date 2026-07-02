"""MARSOUD-PRODUCT-HIERARCHY-01 — Group → Category → Product.

Adds:
  - product_groups + product_categories tables (per company)
  - products.category_id (nullable at DB level for backfill safety)
  - Backfill: every existing product gets attached to a per-company
    default "عام" group + "عام" category so nothing is orphaned.

Revision ID: d4_f5a8b3c9e2d
Revises: d3_e4b7a5c9d8f
"""
from alembic import op
import sqlalchemy as sa


revision = "d4_f5a8b3c9e2d"
down_revision = "d3_e4b7a5c9d8f"
branch_labels = None
depends_on = None


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_table("product_groups"):
        op.create_table(
            "product_groups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("name", sa.String(150), nullable=False),
            sa.Column("is_active", sa.Boolean(),
                      nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(),
                      server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint("company_id", "name",
                                name="uq_product_group_company_name"),
        )
    if not _has_table("product_categories"):
        op.create_table(
            "product_categories",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("group_id", sa.Integer(),
                      sa.ForeignKey("product_groups.id"),
                      nullable=False, index=True),
            sa.Column("name", sa.String(150), nullable=False),
            sa.Column("is_active", sa.Boolean(),
                      nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(),
                      server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint("company_id", "group_id", "name",
                                name="uq_product_category_group_name"),
        )

    if not _has_col("products", "category_id"):
        with op.batch_alter_table("products") as batch:
            batch.add_column(sa.Column(
                "category_id", sa.Integer(),
                sa.ForeignKey(
                    "product_categories.id",
                    name="fk_products_category_id_product_categories",
                ),
                nullable=True,
            ))

    # Backfill: every company that already has products gets an "عام"
    # group + "عام" category, and every product is repointed at it.
    bind = op.get_bind()
    companies = bind.execute(sa.text(
        "SELECT DISTINCT company_id FROM products",
    )).fetchall()
    for row in companies:
        cid = row[0]
        # Ensure default group exists.
        g_row = bind.execute(sa.text(
            "SELECT id FROM product_groups "
            "WHERE company_id = :c AND name = 'عام'"
        ), {"c": cid}).fetchone()
        if g_row:
            group_id = g_row[0]
        else:
            bind.execute(sa.text(
                "INSERT INTO product_groups (company_id, name, is_active) "
                "VALUES (:c, 'عام', 1)"
            ), {"c": cid})
            group_id = bind.execute(sa.text(
                "SELECT id FROM product_groups WHERE company_id = :c AND name = 'عام'"
            ), {"c": cid}).fetchone()[0]

        # Ensure default category exists under the default group.
        cat_row = bind.execute(sa.text(
            "SELECT id FROM product_categories "
            "WHERE company_id = :c AND group_id = :g AND name = 'عام'"
        ), {"c": cid, "g": group_id}).fetchone()
        if cat_row:
            cat_id = cat_row[0]
        else:
            bind.execute(sa.text(
                "INSERT INTO product_categories (company_id, group_id, name, is_active) "
                "VALUES (:c, :g, 'عام', 1)"
            ), {"c": cid, "g": group_id})
            cat_id = bind.execute(sa.text(
                "SELECT id FROM product_categories "
                "WHERE company_id = :c AND group_id = :g AND name = 'عام'"
            ), {"c": cid, "g": group_id}).fetchone()[0]

        # Attach every product without a category to the default cat.
        bind.execute(sa.text(
            "UPDATE products SET category_id = :cat "
            "WHERE company_id = :c AND category_id IS NULL"
        ), {"cat": cat_id, "c": cid})


def downgrade():
    if _has_col("products", "category_id"):
        with op.batch_alter_table("products") as batch:
            batch.drop_column("category_id")
    if _has_table("product_categories"):
        op.drop_table("product_categories")
    if _has_table("product_groups"):
        op.drop_table("product_groups")
