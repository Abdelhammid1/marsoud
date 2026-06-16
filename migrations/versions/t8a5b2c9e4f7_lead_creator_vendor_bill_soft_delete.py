"""MARSOUD-44 follow-up + MARSOUD-52 — lead creator + vendor bill soft delete

Adds:
  - leads.created_by_id       Integer FK → users.id nullable
                              backfilled to assigned_to_id (best guess)
  - vendor_bills.deleted_at   DateTime nullable
  - vendor_bills.deleted_by_id Integer FK → users.id nullable

Revision ID: t8a5b2c9e4f7
Revises: s7e9f4d8b3a1
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = 't8a5b2c9e4f7'
down_revision = 's7e9f4d8b3a1'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    # MARSOUD-44 follow-up — Lead.created_by_id
    if not _has_col("leads", "created_by_id"):
        with op.batch_alter_table("leads", schema=None) as batch_op:
            batch_op.add_column(sa.Column("created_by_id", sa.Integer(),
                                           nullable=True))
            batch_op.create_foreign_key(
                "fk_leads_created_by_id", "users",
                ["created_by_id"], ["id"],
            )
        op.execute("UPDATE leads SET created_by_id = assigned_to_id "
                   "WHERE created_by_id IS NULL")

    # MARSOUD-52 — vendor_bills soft-delete columns
    if not _has_col("vendor_bills", "deleted_at"):
        with op.batch_alter_table("vendor_bills", schema=None) as batch_op:
            batch_op.add_column(sa.Column("deleted_at", sa.DateTime(),
                                           nullable=True))
    if not _has_col("vendor_bills", "deleted_by_id"):
        with op.batch_alter_table("vendor_bills", schema=None) as batch_op:
            batch_op.add_column(sa.Column("deleted_by_id", sa.Integer(),
                                           nullable=True))
            batch_op.create_foreign_key(
                "fk_vendor_bills_deleted_by_id", "users",
                ["deleted_by_id"], ["id"],
            )


def downgrade():
    if _has_col("vendor_bills", "deleted_by_id"):
        with op.batch_alter_table("vendor_bills", schema=None) as batch_op:
            try:
                batch_op.drop_constraint("fk_vendor_bills_deleted_by_id",
                                          type_="foreignkey")
            except Exception:
                pass
            batch_op.drop_column("deleted_by_id")
    if _has_col("vendor_bills", "deleted_at"):
        with op.batch_alter_table("vendor_bills", schema=None) as batch_op:
            batch_op.drop_column("deleted_at")
    if _has_col("leads", "created_by_id"):
        with op.batch_alter_table("leads", schema=None) as batch_op:
            try:
                batch_op.drop_constraint("fk_leads_created_by_id",
                                          type_="foreignkey")
            except Exception:
                pass
            batch_op.drop_column("created_by_id")
