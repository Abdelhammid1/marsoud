"""MARSOUD-VENDOR-SUBCAT (Abdelhamid 2026-07-14).

Creates the vendor_sub_categories table + adds sub_category_id to
vendor_bill_items. Fully backwards-compatible:
  · new column is nullable
  · legacy bill rows keep working unchanged
  · no accounting impact

Revision ID: d9e2f5a8b3c6
Revises: c8d1e4f7a2b5
Create Date: 2026-07-14
"""
from alembic import op
import sqlalchemy as sa


revision = "d9e2f5a8b3c6"
down_revision = "c8d1e4f7a2b5"
branch_labels = None
depends_on = None


def _has_table(name):
    insp = sa.inspect(op.get_bind())
    return name in insp.get_table_names()


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_table("vendor_sub_categories"):
        op.create_table(
            "vendor_sub_categories",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                       sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("vendor_id", sa.Integer(),
                       sa.ForeignKey("vendors.id", ondelete="CASCADE"),
                       nullable=False),
            sa.Column("name", sa.String(length=150), nullable=False),
            sa.Column("is_active", sa.Boolean(),
                       nullable=False, server_default=sa.text("1")),
            sa.Column("created_by_id", sa.Integer(),
                       sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                       nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(),
                       nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("company_id", "vendor_id", "name",
                                 name="uq_vendor_subcat_name"),
        )
        op.create_index("ix_vendor_subcat_company",
                         "vendor_sub_categories", ["company_id"])
        op.create_index("ix_vendor_subcat_vendor",
                         "vendor_sub_categories", ["vendor_id"])
        op.create_index("ix_vendor_subcat_active",
                         "vendor_sub_categories", ["is_active"])

    if not _has_col("vendor_bill_items", "sub_category_id"):
        # SQLite requires named FK constraints inside batch_alter_table.
        with op.batch_alter_table("vendor_bill_items", schema=None) as batch:
            batch.add_column(sa.Column(
                "sub_category_id", sa.Integer(), nullable=True,
            ))
            batch.create_foreign_key(
                "fk_vendor_bill_items_sub_category",
                "vendor_sub_categories",
                ["sub_category_id"], ["id"],
            )
        op.create_index("ix_vendor_bill_items_sub_category",
                         "vendor_bill_items", ["sub_category_id"])


def downgrade():
    if _has_col("vendor_bill_items", "sub_category_id"):
        op.drop_index("ix_vendor_bill_items_sub_category",
                       table_name="vendor_bill_items")
        with op.batch_alter_table("vendor_bill_items", schema=None) as batch:
            batch.drop_constraint(
                "fk_vendor_bill_items_sub_category", type_="foreignkey")
            batch.drop_column("sub_category_id")
    if _has_table("vendor_sub_categories"):
        op.drop_index("ix_vendor_subcat_active",
                       table_name="vendor_sub_categories")
        op.drop_index("ix_vendor_subcat_vendor",
                       table_name="vendor_sub_categories")
        op.drop_index("ix_vendor_subcat_company",
                       table_name="vendor_sub_categories")
        op.drop_table("vendor_sub_categories")
