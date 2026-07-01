"""MARSOUD-CRM-EXPANSION §2 + §5b + §5c — Campaigns, Activities, Contacts.

Creates:
  - campaigns              per-company marketing campaigns
  - lead_activities        touchpoints (call/email/meeting/note) + follow-ups
  - lead_contacts          extra contact persons per lead/customer
  - leads.campaign_id      nullable FK → campaigns.id

All new tables are company-scoped and indexed on (company_id, ...).
Backfill is not needed — existing leads keep campaign_id=NULL.

Revision ID: c5_d3f4e8a7b1c
Revises: c4_b2c8f9e5a3d
"""
from alembic import op
import sqlalchemy as sa

revision = "c5_d3f4e8a7b1c"
down_revision = "c4_b2c8f9e5a3d"
branch_labels = None
depends_on = None


def _has_table(name):
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_table("campaigns"):
        op.create_table(
            "campaigns",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"), nullable=False, index=True),
            sa.Column("name", sa.String(150), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.current_timestamp()),
            sa.Column("created_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.UniqueConstraint("company_id", "name",
                                 name="uq_campaign_company_name"),
        )

    if not _has_table("lead_activities"):
        op.create_table(
            "lead_activities",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"), nullable=False, index=True),
            sa.Column("lead_id", sa.Integer(),
                      sa.ForeignKey("leads.id"), nullable=False, index=True),
            sa.Column("type", sa.String(20), nullable=False, index=True),
            sa.Column("subject", sa.String(255), nullable=True),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("activity_date", sa.DateTime(), nullable=False,
                      server_default=sa.func.current_timestamp()),
            sa.Column("follow_up_date", sa.Date(), nullable=True, index=True),
            sa.Column("created_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.current_timestamp()),
        )

    if not _has_table("lead_contacts"):
        op.create_table(
            "lead_contacts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"), nullable=False, index=True),
            sa.Column("lead_id", sa.Integer(),
                      sa.ForeignKey("leads.id"), nullable=True, index=True),
            sa.Column("customer_id", sa.Integer(),
                      sa.ForeignKey("customers.id"), nullable=True, index=True),
            sa.Column("name", sa.String(150), nullable=False),
            sa.Column("role", sa.String(100), nullable=True),
            sa.Column("email", sa.String(200), nullable=True),
            sa.Column("phone", sa.String(30), nullable=True),
            sa.Column("is_primary", sa.Boolean(), nullable=False,
                      server_default="0"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.current_timestamp()),
        )

    if not _has_col("leads", "campaign_id"):
        with op.batch_alter_table("leads") as batch:
            batch.add_column(sa.Column(
                "campaign_id", sa.Integer(),
                sa.ForeignKey("campaigns.id",
                              name="fk_leads_campaign_id_campaigns"),
                nullable=True,
            ))


def downgrade():
    if _has_col("leads", "campaign_id"):
        with op.batch_alter_table("leads") as batch:
            batch.drop_column("campaign_id")
    for t in ("lead_contacts", "lead_activities", "campaigns"):
        if _has_table(t):
            op.drop_table(t)
