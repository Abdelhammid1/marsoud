"""company_smtp last_send_* columns for ticket's AC #8

MARSOUD-SMTP-PER-COMPANY-01 (2026-10-18, follow-up) — the ticket's
AC #8 says a company-SMTP failure must be "logged AND shown to the
authorized user".  Logs alone are not enough — the operator needs
a persistent UI surface.  Three columns track the last live-send
attempt against the per-company config: when, ok/fail, and the
error text (never the password).

NULL for every existing row; the UI treats absence as "no sends
yet, no outcome to show".

Revision ID: e5f8c71b2a19
Revises: b7f3e215c984
Create Date: 2026-10-18 17:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = "e5f8c71b2a19"
down_revision = "b7f3e215c984"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("company_smtp") as batch:
        batch.add_column(sa.Column("last_send_at", sa.DateTime(),
                                     nullable=True))
        batch.add_column(sa.Column("last_send_ok", sa.Boolean(),
                                     nullable=True))
        batch.add_column(sa.Column("last_send_error", sa.Text(),
                                     nullable=True))


def downgrade():
    with op.batch_alter_table("company_smtp") as batch:
        batch.drop_column("last_send_error")
        batch.drop_column("last_send_ok")
        batch.drop_column("last_send_at")
