"""MARSOUD-CONSENT-AUDIT-LOG (Abdelhamid 2026-07-22).

Immutable, append-only history table for every legal-consent event.
Ticket B stored the LAST acceptance on `users` (one timestamp + one
version) — that's not audit-grade. This table keeps every
acceptance forever so legal / regulators can reconstruct who
accepted what, when, from where.

Backfill: writes a synthetic 'backfill' event per existing user
with users.terms_accepted_at NOT NULL so we don't lose the history
we already had. NULL company_id on those because the acceptance
predates our per-tenant tracking.

Revision ID: l6a9c2e5b8d1
Revises: k5f8b1d4c7e2
Create Date: 2026-07-22
"""
from alembic import op
import sqlalchemy as sa


revision = 'l6a9c2e5b8d1'
down_revision = 'k5f8b1d4c7e2'
branch_labels = None
depends_on = None


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "consent_events" not in insp.get_table_names():
        op.create_table(
            "consent_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("company_id", sa.Integer(),
                      sa.ForeignKey("companies.id"),
                      nullable=True, index=True),
            sa.Column("consent_type", sa.String(30),
                      nullable=False),   # "terms" / "privacy"
            sa.Column("document_version", sa.String(20),
                      nullable=False),
            sa.Column("ip_address", sa.String(45)),
            sa.Column("user_agent", sa.String(500)),
            sa.Column("source", sa.String(30), nullable=False),
            # signup / reaccept / backfill / manual
            sa.Column("created_at", sa.DateTime(),
                      nullable=False,
                      server_default=sa.func.current_timestamp(),
                      index=True),
        )
    # Backfill from users.terms_accepted_at.
    op.execute(
        "INSERT INTO consent_events "
        "(user_id, company_id, consent_type, document_version, "
        " source, created_at) "
        "SELECT id, NULL, 'terms', "
        "  COALESCE(terms_version, 'v0'), 'backfill', "
        "  terms_accepted_at "
        "FROM users WHERE terms_accepted_at IS NOT NULL"
    )


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "consent_events" in insp.get_table_names():
        op.drop_table("consent_events")
