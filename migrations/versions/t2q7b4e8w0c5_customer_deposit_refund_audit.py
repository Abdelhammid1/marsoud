"""MARSOUD-DEPOSIT-AUDIT-01 (2026-08-06) — record who refunded a
customer deposit and when.

Follow-up on the customer-deposit permissions ticket. Reception
already has an audit trail — CustomerDeposit.created_by_id is set at
record_deposit() time by the user who conducted it. Refund had no
matching column: even after the permission fix, an incident
predating the fix left no name attached to the person who moved the
money out.

Additive. Both columns nullable so existing refunded deposits stay
NULL (the ticket accepts this: "الودائع القديمة … هتفضل بدون منفّذ
مسجّل — طبيعي"). Every future refund gets both fields stamped by
services/deposits.py::refund().

Revision ID: t2q7b4e8w0c5
Revises: s1p6a3d7v9b4
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa


revision = 't2q7b4e8w0c5'
down_revision = 's1p6a3d7v9b4'
branch_labels = None
depends_on = None


TABLE = "customer_deposits"
FK_NAME = "fk_customer_deposits_refunded_by_users"


def _cols():
    insp = sa.inspect(op.get_bind())
    if TABLE not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(TABLE)}


def upgrade():
    bind = op.get_bind()
    if TABLE not in sa.inspect(bind).get_table_names():
        return
    have = _cols()
    if "refunded_by_id" not in have:
        op.add_column(TABLE, sa.Column(
            "refunded_by_id", sa.Integer, nullable=True))
    if "refunded_at" not in have:
        op.add_column(TABLE, sa.Column(
            "refunded_at", sa.DateTime, nullable=True))
    if bind.dialect.name == "postgresql":
        existing = {fk["name"] for fk
                    in sa.inspect(bind).get_foreign_keys(TABLE)}
        if FK_NAME not in existing:
            op.create_foreign_key(FK_NAME, TABLE, "users",
                                  ["refunded_by_id"], ["id"])


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        try:
            op.drop_constraint(FK_NAME, TABLE, type_="foreignkey")
        except Exception:
            pass
    have = _cols()
    for c in ("refunded_at", "refunded_by_id"):
        if c in have:
            try:
                op.drop_column(TABLE, c)
            except Exception:
                pass
