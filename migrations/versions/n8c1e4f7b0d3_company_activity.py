"""MARSOUD-INACTIVE-COMPANIES-MONITORING (Abdelhamid 2026-07-22).

Adds companies.last_activity_at DATETIME NULL (indexed). Stamped by
start_session() on every user login for that tenant so
/admin/companies/inactive can filter by inactivity window without
scanning UserSession every time.

Backfill: seeds each company with MAX(UserSession.login_at) across
its users so existing tenants appear at their real last-activity
timestamp instead of NULL (which would falsely flag every company
as "never used").

Revision ID: n8c1e4f7b0d3
Revises: m7b0d3e6a9c4
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'n8c1e4f7b0d3'
down_revision = 'm7b0d3e6a9c4'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if not _has_col("companies", "last_activity_at"):
        with op.batch_alter_table("companies", schema=None) as batch:
            batch.add_column(sa.Column(
                "last_activity_at", sa.DateTime(), nullable=True))
            batch.create_index(
                "ix_companies_last_activity_at",
                ["last_activity_at"])

    # Backfill from user_sessions.
    op.execute(
        "UPDATE companies SET last_activity_at = "
        "(SELECT MAX(us.login_at) FROM user_sessions us "
        " WHERE us.company_id = companies.id)"
    )


def downgrade():
    if _has_col("companies", "last_activity_at"):
        with op.batch_alter_table("companies", schema=None) as batch:
            try:
                batch.drop_index("ix_companies_last_activity_at")
            except Exception:
                pass
            batch.drop_column("last_activity_at")
