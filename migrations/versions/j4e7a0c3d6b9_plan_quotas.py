"""MARSOUD-QUOTAS (Abdelhamid 2026-07-22).

Generic Plan-Quotas system. Each Plan can attach any number of
Quota rows — one per (quota_type). Quota types are open strings
(no ALTER on SQLite when we add a new type). v1 types:
  · users
  · ai_tokens_month
  · storage_bytes
  · branches

Also creates:
  · ai_token_usage — one row per Anthropic request (company/user/
    provider/model/input/output/total/created_at) — populated via
    the response.usage hook in the agent path.
  · employee_ai_caps — optional per-employee monthly cap (subset
    of the company's overall AI tokens quota).
  · quota_notifications_sent — dedupe at (company_id, quota_type,
    threshold_pct, cycle_month) so the 80/90/100 alerts fire at
    most once per cycle each.

Revision ID: j4e7a0c3d6b9
Revises: i3d6e9b2c5a8
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'j4e7a0c3d6b9'
down_revision = 'i3d6e9b2c5a8'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())

    if "quotas" not in insp.get_table_names():
        op.create_table(
            "quotas",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("plan_id", sa.Integer(),
                      sa.ForeignKey("plans.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("quota_type", sa.String(40), nullable=False),
            sa.Column("included_amount", sa.BigInteger(),
                      nullable=False, server_default="0"),
            sa.Column("enforcement_mode", sa.String(20),
                      nullable=False, server_default="UNLIMITED"),
            sa.Column("price_per_extra_unit", sa.Numeric(15, 4),
                      nullable=True),
            sa.UniqueConstraint(
                "plan_id", "quota_type",
                name="uq_quotas_plan_type"),
        )

    if "ai_token_usage" not in insp.get_table_names():
        op.create_table(
            "ai_token_usage",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id"),
                      nullable=True),
            sa.Column("provider", sa.String(20),
                      nullable=False, server_default="anthropic"),
            sa.Column("model", sa.String(80)),
            sa.Column("input_tokens", sa.Integer(),
                      nullable=False, server_default="0"),
            sa.Column("output_tokens", sa.Integer(),
                      nullable=False, server_default="0"),
            sa.Column("total_tokens", sa.Integer(),
                      nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp(),
                      index=True),
        )

    if "employee_ai_caps" not in insp.get_table_names():
        op.create_table(
            "employee_ai_caps",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id"),
                      nullable=False),
            sa.Column("monthly_cap", sa.BigInteger(),
                      nullable=False),
            sa.UniqueConstraint(
                "company_id", "user_id",
                name="uq_employee_ai_cap"),
        )

    if "quota_notifications_sent" not in insp.get_table_names():
        op.create_table(
            "quota_notifications_sent",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("quota_type", sa.String(40), nullable=False),
            sa.Column("threshold_pct", sa.Integer(), nullable=False),
            sa.Column("cycle_month", sa.String(7), nullable=False),
            sa.Column("sent_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
            sa.UniqueConstraint(
                "company_id", "quota_type", "threshold_pct", "cycle_month",
                name="uq_quota_notif"),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    for tname in ("quota_notifications_sent", "employee_ai_caps",
                   "ai_token_usage", "quotas"):
        if tname in insp.get_table_names():
            op.drop_table(tname)
