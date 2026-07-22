"""MARSOUD-CUSTOMER-BROADCAST-CENTER (Abdelhamid 2026-07-22).

broadcasts table + broadcast_recipients. Super-admin composes a
message + picks an audience filter + hits send; every matching
user gets one in-app Notification + optionally one email.

Recipients table isn't strictly needed for correctness (the audit
trail lives on Notifications) but gives us a per-user delivery
receipt for the "how many were sent" number on the admin list.

Revision ID: p0e3a6c9b2d5
Revises: o9d2f5a8c1e4
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'p0e3a6c9b2d5'
down_revision = 'o9d2f5a8c1e4'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "broadcasts" not in insp.get_table_names():
        op.create_table(
            "broadcasts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("body_html", sa.Text(), nullable=False),
            sa.Column("audience_filter", sa.Text(),
                      nullable=False),   # JSON: {kind, plan_id, status}
            sa.Column("channels", sa.String(40),
                      nullable=False, server_default="INAPP"),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("sent_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("target_count", sa.Integer(),
                      nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "broadcasts" in insp.get_table_names():
        op.drop_table("broadcasts")
