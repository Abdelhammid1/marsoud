"""MARSOUD-EVAL-CATEGORY-WEIGHT — per-(cycle, employee, category)
weight override.

Follow-up to d9f4a3b8c5e2. The 60/25/15 blend Abdelhamid's spec
called out was hardcoded in compute_score. He asked for it to be
editable per employee since different roles carry different
emphasis — e.g. a fresh hire might weight growth 60%, execution
40%, targets 0%.

The override lives in its own table so a cycle can still leave
some (or all) employees on the default 60/25/15 without needing
placeholder rows. compute_score reads the overrides first and
falls back to the defaults when a row is missing.

Revision ID: e0a7b4c6f2d8
Revises: d9f4a3b8c5e2
"""
from alembic import op
import sqlalchemy as sa


revision = "e0a7b4c6f2d8"
down_revision = "d9f4a3b8c5e2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "employee_category_weights",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("cycle_id", sa.Integer,
                    sa.ForeignKey("evaluation_cycles.id",
                                     ondelete="CASCADE"),
                    nullable=False, index=True),
        sa.Column("employee_id", sa.Integer,
                    sa.ForeignKey("employees.id", ondelete="CASCADE"),
                    nullable=False, index=True),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("weight_pct", sa.Numeric(6, 2), nullable=False,
                    server_default="0"),
        sa.UniqueConstraint(
            "cycle_id", "employee_id", "category",
            name="uq_ecw_cycle_employee_category",
        ),
    )


def downgrade():
    op.drop_table("employee_category_weights")
