"""MARSOUD-HELP-CENTER-01 (Abdelhamid 2026-07-24).

In-product help articles, managed as a CMS from Super Admin.
Three additive tables — no impact on existing data.

  · help_articles      — one row per module (invoices, inventory…).
  · help_examples      — 0..N examples per article, ordered.
  · help_media         — 0..N images or YouTube/Vimeo embeds.

The route layer 404s when no published article exists, so brand-
new deploys simply have no /help/<module_key> pages until the
super-admin publishes them. Idempotent — existing tables skipped.

Revision ID: q1f4b7d0c3e6
Revises: p0e3a6c9b2d5
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa


revision = 'q1f4b7d0c3e6'
down_revision = 'p0e3a6c9b2d5'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "help_articles" not in insp.get_table_names():
        op.create_table(
            "help_articles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("module_key", sa.String(60), nullable=False,
                      index=True),
            sa.Column("title_ar", sa.String(200), nullable=False),
            sa.Column("title_en", sa.String(200), nullable=True),
            sa.Column("goal", sa.Text(), nullable=True),
            sa.Column("general_explanation", sa.Text(), nullable=True),
            sa.Column("tips", sa.Text(), nullable=True),          # JSON
            sa.Column("related_module_keys", sa.Text(),
                      nullable=True),                              # JSON
            sa.Column("display_order", sa.Integer(),
                      nullable=False, server_default="0"),
            sa.Column("is_published", sa.Boolean(),
                      nullable=False, server_default=sa.text("0")),
            sa.Column("created_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
            sa.Column("updated_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp()),
        )
    if "help_examples" not in insp.get_table_names():
        op.create_table(
            "help_examples",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("article_id", sa.Integer(),
                      sa.ForeignKey("help_articles.id",
                                     ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("display_order", sa.Integer(),
                      nullable=False, server_default="0"),
        )
    if "help_media" not in insp.get_table_names():
        op.create_table(
            "help_media",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("article_id", sa.Integer(),
                      sa.ForeignKey("help_articles.id",
                                     ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("type", sa.String(20), nullable=False),
            # For IMAGE: file_path is the uuid-prefixed key under
            # private_uploads/help_media/. For YOUTUBE/VIMEO/LINK:
            # url holds the source URL; file_path is NULL.
            sa.Column("file_path", sa.String(400), nullable=True),
            sa.Column("url", sa.String(500), nullable=True),
            sa.Column("caption", sa.String(400), nullable=True),
            sa.Column("display_order", sa.Integer(),
                      nullable=False, server_default="0"),
        )


def downgrade():
    insp = sa.inspect(op.get_bind())
    for t in ("help_media", "help_examples", "help_articles"):
        if t in insp.get_table_names():
            op.drop_table(t)
