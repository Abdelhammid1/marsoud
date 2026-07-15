"""MARSOUD-CRM-STATUS-ACTIVITY-SPLIT (Abdelhamid 2026-07-15).

Adds lead_activities.outcome column + extends the LeadActivityType
enum with 5 new kinds (WhatsApp, Visit, File-sent, Quote-sent,
Contract-signed) to support the ticket's separation of pipeline
milestones from touchpoint records.

Revision ID: e5c8f1a4b7d0
Revises: d9e2f5a8b3c6
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa


revision = "e5c8f1a4b7d0"
down_revision = "d9e2f5a8b3c6"
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("lead_activities", "outcome"):
        with op.batch_alter_table("lead_activities", schema=None) as batch:
            batch.add_column(sa.Column(
                "outcome", sa.String(length=60), nullable=True,
            ))
        op.create_index("ix_lead_activities_outcome",
                         "lead_activities", ["outcome"])

    # Postgres native ENUM extension for the 5 new types. SQLite stores
    # enums as VARCHAR so no schema change is needed there.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for value in ("WHATSAPP", "VISIT", "FILE_SENT",
                       "QUOTE_SENT", "CONTRACT_SIGNED"):
            with op.get_context().autocommit_block():
                op.execute(
                    f"ALTER TYPE leadactivitytype ADD VALUE IF NOT EXISTS '{value}'"
                )


def downgrade():
    if _has_col("lead_activities", "outcome"):
        op.drop_index("ix_lead_activities_outcome",
                       table_name="lead_activities")
        with op.batch_alter_table("lead_activities", schema=None) as batch:
            batch.drop_column("outcome")
    # Enum values can't be safely removed on Postgres; leave them.
