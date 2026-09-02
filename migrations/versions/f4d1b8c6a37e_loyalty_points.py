"""MARSOUD-LOYALTY-POINTS-01 — loyalty tables + tenant/customer/invoice columns.

Revision ID: f4d1b8c6a37e
Revises: e2b8c471a9d3
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa

revision = "f4d1b8c6a37e"
down_revision = "e2b8c471a9d3"
branch_labels = None
depends_on = None

TXN = "loyalty_point_transactions"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_col(table, col):
    if not _has_table(table):
        return False
    return col in {c["name"] for c in _inspector().get_columns(table)}


def upgrade():
    # ─ companies — 3 columns ───────────────────────────────
    if _has_table("companies"):
        with op.batch_alter_table("companies") as batch:
            if not _has_col("companies", "loyalty_enabled"):
                batch.add_column(sa.Column(
                    "loyalty_enabled", sa.Boolean, nullable=False,
                    server_default=sa.false()))
            if not _has_col("companies", "loyalty_earn_rate"):
                batch.add_column(sa.Column(
                    "loyalty_earn_rate", sa.Numeric(10, 2),
                    nullable=False, server_default="10"))
            if not _has_col("companies", "loyalty_redemption_value"):
                batch.add_column(sa.Column(
                    "loyalty_redemption_value", sa.Numeric(10, 4),
                    nullable=False, server_default="0.10"))

    # ─ customers — 1 column ────────────────────────────────
    if _has_table("customers") and not _has_col(
            "customers", "loyalty_points_balance"):
        with op.batch_alter_table("customers") as batch:
            batch.add_column(sa.Column(
                "loyalty_points_balance", sa.Integer,
                nullable=False, server_default="0"))

    # ─ invoices — 3 columns ────────────────────────────────
    if _has_table("invoices"):
        with op.batch_alter_table("invoices") as batch:
            if not _has_col("invoices", "loyalty_points_earned"):
                batch.add_column(sa.Column(
                    "loyalty_points_earned", sa.Integer,
                    nullable=False, server_default="0"))
            if not _has_col("invoices", "loyalty_points_redeemed"):
                batch.add_column(sa.Column(
                    "loyalty_points_redeemed", sa.Integer,
                    nullable=False, server_default="0"))
            if not _has_col("invoices", "loyalty_points_awarded_at"):
                batch.add_column(sa.Column(
                    "loyalty_points_awarded_at", sa.DateTime,
                    nullable=True))

    # ─ loyalty_point_transactions ──────────────────────────
    if not _has_table(TXN):
        op.create_table(
            TXN,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("company_id", sa.Integer,
                        sa.ForeignKey("companies.id"),
                        nullable=False, index=True),
            sa.Column("customer_id", sa.Integer,
                        sa.ForeignKey("customers.id"),
                        nullable=False, index=True),
            sa.Column("points_delta", sa.Integer, nullable=False),
            sa.Column("reason", sa.String(30), nullable=False),
            sa.Column("source_type", sa.String(30), nullable=True),
            sa.Column("source_id", sa.Integer, nullable=True),
            sa.Column("balance_after", sa.Integer, nullable=False),
            sa.Column("reason_note", sa.Text, nullable=True),
            sa.Column("actor_id", sa.Integer,
                        sa.ForeignKey("users.id",
                                        name="fk_loyalty_txn_actor"),
                        nullable=True),
            sa.Column("created_at", sa.DateTime,
                        server_default=sa.func.current_timestamp(),
                        nullable=False),
        )


def downgrade():
    if _has_table(TXN):
        op.drop_table(TXN)
    for col in ("loyalty_points_awarded_at",
                 "loyalty_points_redeemed",
                 "loyalty_points_earned"):
        if _has_table("invoices") and _has_col("invoices", col):
            with op.batch_alter_table("invoices") as batch:
                batch.drop_column(col)
    if _has_table("customers") and _has_col(
            "customers", "loyalty_points_balance"):
        with op.batch_alter_table("customers") as batch:
            batch.drop_column("loyalty_points_balance")
    for col in ("loyalty_redemption_value", "loyalty_earn_rate",
                 "loyalty_enabled"):
        if _has_table("companies") and _has_col("companies", col):
            with op.batch_alter_table("companies") as batch:
                batch.drop_column(col)
