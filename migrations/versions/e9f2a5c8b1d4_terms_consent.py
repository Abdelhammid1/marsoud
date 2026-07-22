"""MARSOUD-TERMS-CONSENT (Abdelhamid 2026-07-22).

Adds two columns to `users` for the legal-consent audit trail:

  · terms_accepted_at DATETIME NULL — when they clicked "أوافق".
  · terms_version    VARCHAR(20) NULL — which version of the T&C /
    Privacy pair they agreed to. When super-admin publishes a new
    version, users whose stored version doesn't match get re-prompted
    on their next request via the middleware.

Content of the T&C + Privacy pages themselves lives in
platform_settings under keys terms_content_html,
privacy_content_html, and the current version string
terms_version. Super-admin edits via /admin/legal.

Backfill: users created before this migration get NULL/NULL so the
middleware nudges them to accept on next login (they're
grandfathered on the version bump but must click through once).

Revision ID: e9f2a5c8b1d4
Revises: d8e1f4a9c3b2
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'e9f2a5c8b1d4'
down_revision = 'd8e1f4a9c3b2'
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch:
        if not _has_col("users", "terms_accepted_at"):
            batch.add_column(sa.Column(
                "terms_accepted_at", sa.DateTime(), nullable=True))
        if not _has_col("users", "terms_version"):
            batch.add_column(sa.Column(
                "terms_version", sa.String(20), nullable=True))


def downgrade():
    with op.batch_alter_table("users", schema=None) as batch:
        if _has_col("users", "terms_version"):
            batch.drop_column("terms_version")
        if _has_col("users", "terms_accepted_at"):
            batch.drop_column("terms_accepted_at")
