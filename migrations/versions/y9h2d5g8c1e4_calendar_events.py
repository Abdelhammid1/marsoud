"""MARSOUD-CALENDAR-MANUAL-EVENTS (Abdelhamid 2026-07-29).

New calendar_events table for user-added events on /calendar/.
Cross-tenant scoped via company_id (indexed). starts_at indexed
so the window filter in calendar.index() stays cheap. Soft-delete
via is_deleted so we keep audit history without confusing the
timeline.

Revision ID: y9h2d5g8c1e4
Revises: x8g1c4f7b0d3
Create Date: 2026-07-29
"""
from alembic import op
import sqlalchemy as sa


revision = 'y9h2d5g8c1e4'
down_revision = 'x8g1c4f7b0d3'
branch_labels = None
depends_on = None


def _has_table(insp, name):
    return name in insp.get_table_names()


def upgrade():
    insp = sa.inspect(op.get_bind())
    if not _has_table(insp, "calendar_events"):
        op.create_table(
            "calendar_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("created_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("starts_at", sa.DateTime(), nullable=False),
            sa.Column("ends_at", sa.DateTime(), nullable=True),
            sa.Column("location", sa.String(500), nullable=True),
            sa.Column("reminder_minutes_before", sa.Integer(),
                      nullable=True),
            sa.Column("is_deleted", sa.Boolean(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_calendar_events_company_id",
                        "calendar_events", ["company_id"])
        op.create_index("ix_calendar_events_starts_at",
                        "calendar_events", ["starts_at"])
        op.create_index("ix_calendar_events_is_deleted",
                        "calendar_events", ["is_deleted"])


def downgrade():
    insp = sa.inspect(op.get_bind())
    if _has_table(insp, "calendar_events"):
        op.drop_index("ix_calendar_events_is_deleted",
                      table_name="calendar_events")
        op.drop_index("ix_calendar_events_starts_at",
                      table_name="calendar_events")
        op.drop_index("ix_calendar_events_company_id",
                      table_name="calendar_events")
        op.drop_table("calendar_events")
