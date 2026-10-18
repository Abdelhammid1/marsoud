"""company_smtp — per-tenant SMTP configuration

MARSOUD-SMTP-PER-COMPANY-01 (2026-10-18) — one row per company
(UNIQUE on company_id) holding the SMTP host/port/encryption +
encrypted password + from address.  When present + is_enabled, the
central `send_email()` service uses this instead of the global
`.env` SMTP config.  Absent → falls back to the global default,
preserving today's behavior for every existing tenant with zero
migration data.

Revision ID: b7f3e215c984
Revises: d4a9b7e216f8
Create Date: 2026-10-18 16:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = "b7f3e215c984"
down_revision = "d4a9b7e216f8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "company_smtp",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(),
                   sa.ForeignKey("companies.id"),
                   nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("encryption", sa.String(16), nullable=False,
                   server_default="STARTTLS"),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("password_encrypted", sa.LargeBinary(),
                   nullable=True),
        sa.Column("from_email", sa.String(255), nullable=False),
        sa.Column("from_name", sa.String(255), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False,
                   server_default=sa.true()),
        sa.Column("last_test_at", sa.DateTime(), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    # UNIQUE on company_id — one SMTP config per tenant.
    op.create_index(
        "ix_company_smtp_company_id_unique",
        "company_smtp", ["company_id"], unique=True,
    )


def downgrade():
    op.drop_index("ix_company_smtp_company_id_unique",
                   table_name="company_smtp")
    op.drop_table("company_smtp")
