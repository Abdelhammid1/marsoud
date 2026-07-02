"""MARSOUD-MANUFACTURING-01 — BOM + work orders.

Revision ID: d3_e4b7a5c9d8f
Revises: d2_c9f4a8e2b6d
"""
from alembic import op
import sqlalchemy as sa


revision = "d3_e4b7a5c9d8f"
down_revision = "d2_c9f4a8e2b6d"
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())

    if "bill_of_materials" not in insp.get_table_names():
        op.create_table(
            "bill_of_materials",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("product_variant_id", sa.Integer(),
                      sa.ForeignKey("product_variants.id"),
                      nullable=False, index=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("is_active", sa.Boolean(),
                      nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(),
                      server_default=sa.func.current_timestamp()),
        )

    if "bom_lines" not in insp.get_table_names():
        op.create_table(
            "bom_lines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("bom_id", sa.Integer(),
                      sa.ForeignKey("bill_of_materials.id",
                                    ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("component_variant_id", sa.Integer(),
                      sa.ForeignKey("product_variants.id"),
                      nullable=False),
            sa.Column("qty_per_unit", sa.Numeric(15, 4), nullable=False),
        )

    if "work_orders" not in insp.get_table_names():
        op.create_table(
            "work_orders",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("number", sa.String(30), nullable=False, index=True),
            sa.Column("bom_id", sa.Integer(),
                      sa.ForeignKey("bill_of_materials.id"),
                      nullable=False),
            sa.Column("warehouse_id", sa.Integer(),
                      sa.ForeignKey("warehouses.id"), nullable=False),
            sa.Column("quantity_to_produce", sa.Numeric(15, 4),
                      nullable=False),
            sa.Column("status", sa.String(20), nullable=False,
                      server_default="DRAFT", index=True),
            sa.Column("direct_labor_cost", sa.Numeric(15, 4),
                      nullable=False, server_default="0"),
            sa.Column("overhead_cost", sa.Numeric(15, 4),
                      nullable=False, server_default="0"),
            sa.Column("journal_entry_id", sa.Integer(),
                      sa.ForeignKey("journal_entries.id")),
            sa.Column("created_by", sa.Integer(),
                      sa.ForeignKey("users.id")),
            sa.Column("completed_at", sa.DateTime()),
            sa.Column("created_at", sa.DateTime(),
                      server_default=sa.func.current_timestamp(),
                      nullable=False),
        )

    if "work_order_consumption" not in insp.get_table_names():
        op.create_table(
            "work_order_consumption",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("work_order_id", sa.Integer(),
                      sa.ForeignKey("work_orders.id",
                                    ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("component_variant_id", sa.Integer(),
                      sa.ForeignKey("product_variants.id"),
                      nullable=False),
            sa.Column("qty_consumed", sa.Numeric(15, 4), nullable=False),
            sa.Column("unit_cost_at_time", sa.Numeric(15, 4),
                      nullable=False),
            sa.Column("created_at", sa.DateTime(),
                      server_default=sa.func.current_timestamp(),
                      nullable=False),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    for t in ("work_order_consumption", "work_orders",
                "bom_lines", "bill_of_materials"):
        if t in insp.get_table_names():
            op.drop_table(t)
