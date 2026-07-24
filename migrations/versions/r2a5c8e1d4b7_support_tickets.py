"""MARSOUD-SUPPORT-TICKETS-01 (Abdelhamid 2026-07-24).

Cross-tenant support ticketing between customer companies and
Manasty's support team. Three additive tables — every other table
in the app stays company_id-scoped. The cross-tenant read is
enforced surgically at the decorator level, NOT via super-admin.

  · support_tickets       — one per customer request; scoped to the
                            customer's company_id.
  · support_ticket_comments — threaded replies + internal notes.
  · support_ticket_audits — append-only trail of status/assign/etc.

Revision ID: r2a5c8e1d4b7
Revises: q1f4b7d0c3e6
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa


revision = 'r2a5c8e1d4b7'
down_revision = 'q1f4b7d0c3e6'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "support_tickets" not in insp.get_table_names():
        op.create_table(
            "support_tickets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("created_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=False),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("status", sa.String(30), nullable=False,
                      server_default="OPEN", index=True),
            sa.Column("priority", sa.String(20), nullable=False,
                      server_default="MEDIUM"),
            sa.Column("assigned_to_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
        )
    if "support_ticket_comments" not in insp.get_table_names():
        op.create_table(
            "support_ticket_comments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("ticket_id", sa.Integer(),
                      sa.ForeignKey("support_tickets.id",
                                     ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("attachment_url", sa.String(400), nullable=True),
            sa.Column("attachment_name", sa.String(200), nullable=True),
            sa.Column("is_internal", sa.Boolean(),
                      nullable=False, server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
        )
    if "support_ticket_audits" not in insp.get_table_names():
        op.create_table(
            "support_ticket_audits",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("ticket_id", sa.Integer(),
                      sa.ForeignKey("support_tickets.id",
                                     ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("actor_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=False),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("old_value", sa.String(200), nullable=True),
            sa.Column("new_value", sa.String(200), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    for t in ("support_ticket_audits", "support_ticket_comments",
               "support_tickets"):
        if t in insp.get_table_names():
            op.drop_table(t)
