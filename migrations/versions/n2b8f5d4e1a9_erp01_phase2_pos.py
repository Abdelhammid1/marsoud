"""MARSOUD-ERP-01 Phase 2 — POS columns + customer_id nullable + VOIDED status

Revision ID: n2b8f5d4e1a9
Revises: m1a4e7c9b3f6
Create Date: 2026-06-11 22:00:00

Columns added to invoices:
  - source            "MANUAL" / "POS" — backfill existing as "MANUAL".
  - cashier_id        FK → users.id, nullable. Set only when source=POS.
  - cash_received     Numeric(15,4), nullable. POS-only; powers change_due.
  - voided_at         DateTime nullable.
  - voided_by_id      FK → users.id nullable.
  - void_reason       Text nullable.

Existing column changed:
  - customer_id       ALTER to nullable. Walk-in POS orders have NULL.

InvoiceStatus enum gains "VOIDED" — the column is VARCHAR with no CHECK
constraint, so no DDL needed; the Python enum addition is sufficient.

Idempotent.
"""
from alembic import op
import sqlalchemy as sa


revision = "n2b8f5d4e1a9"
down_revision = "m1a4e7c9b3f6"
branch_labels = None
depends_on = None


def _ins():
    return sa.inspect(op.get_bind())


def _has_col(t, c):
    if t not in _ins().get_table_names():
        return False
    return c in {col["name"] for col in _ins().get_columns(t)}


def _col_nullable(t, c):
    if t not in _ins().get_table_names():
        return None
    for col in _ins().get_columns(t):
        if col["name"] == c:
            return col.get("nullable", True)
    return None


def upgrade():
    if not _has_col("invoices", "source"):
        with op.batch_alter_table("invoices") as batch:
            batch.add_column(sa.Column(
                "source", sa.String(20),
                nullable=False, server_default="MANUAL",
            ))
        # The server_default backfills existing rows automatically.

    if not _has_col("invoices", "cashier_id"):
        with op.batch_alter_table("invoices") as batch:
            batch.add_column(sa.Column(
                "cashier_id", sa.Integer,
                sa.ForeignKey("users.id", name="fk_invoices_cashier_id"),
                nullable=True,
            ))

    if not _has_col("invoices", "cash_received"):
        with op.batch_alter_table("invoices") as batch:
            batch.add_column(sa.Column(
                "cash_received", sa.Numeric(15, 4), nullable=True,
            ))

    if not _has_col("invoices", "voided_at"):
        with op.batch_alter_table("invoices") as batch:
            batch.add_column(sa.Column(
                "voided_at", sa.DateTime, nullable=True,
            ))

    if not _has_col("invoices", "voided_by_id"):
        with op.batch_alter_table("invoices") as batch:
            batch.add_column(sa.Column(
                "voided_by_id", sa.Integer,
                sa.ForeignKey("users.id", name="fk_invoices_voided_by_id"),
                nullable=True,
            ))

    if not _has_col("invoices", "void_reason"):
        with op.batch_alter_table("invoices") as batch:
            batch.add_column(sa.Column(
                "void_reason", sa.Text, nullable=True,
            ))

    # ── ALTER customer_id to nullable (SQLite needs batch recreate) ──
    if _col_nullable("invoices", "customer_id") is False:
        with op.batch_alter_table("invoices") as batch:
            batch.alter_column(
                "customer_id",
                existing_type=sa.Integer,
                nullable=True,
            )

    # Index on source — cashier-sales queries filter by it heavily.
    try:
        op.create_index(
            "ix_invoices_source", "invoices", ["source"], unique=False,
        )
    except Exception:
        pass


def downgrade():
    try:
        op.drop_index("ix_invoices_source", table_name="invoices")
    except Exception:
        pass
    for c in ("void_reason", "voided_by_id", "voided_at",
              "cash_received", "cashier_id", "source"):
        if _has_col("invoices", c):
            try:
                with op.batch_alter_table("invoices") as batch:
                    batch.drop_column(c)
            except Exception:
                pass
    # NB: leaving customer_id nullable on downgrade — re-NOT-NULL would
    # break any walk-in rows the user wrote.
