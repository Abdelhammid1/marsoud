"""MARSOUD-VBILL-REFUND-STATUS (2026-08-04).

VendorBillStatus gains REFUNDED and PARTIALLY_REFUNDED so a purchase
return marks the bill it came from, the way InvoiceStatus already does
on the customer side.

The enum change is only a schema step on Postgres (native ENUM type —
the initial schema created it as sa.Enum(..., name='vendorbillstatus')).
SQLite stores enums as plain VARCHAR with no CHECK constraint, so no DDL
is needed there. Same shape as b7c0d3e6f9a2, which added NO_RESPONSE to
leadstatus.

Revision ID: e5n8j1p4i9k0
Revises: d4m7i0o3h8j9
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa


revision = 'e5n8j1p4i9k0'
down_revision = 'd4m7i0o3h8j9'
branch_labels = None
depends_on = None


_NEW_VALUES = ("REFUNDED", "PARTIALLY_REFUNDED")


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name

    # ─── Enum expansion — Postgres only. ────────────────────────────
    if dialect == "postgresql":
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block
        # on older Postgres versions, so run it with AUTOCOMMIT.
        with op.get_context().autocommit_block():
            for value in _NEW_VALUES:
                op.execute(
                    "ALTER TYPE vendorbillstatus "
                    f"ADD VALUE IF NOT EXISTS '{value}'"
                )


def downgrade():
    # Enum values can't be removed cleanly on Postgres (would require
    # rebuilding the type and rewriting every dependent column), and on
    # SQLite there is nothing to undo. Deliberate no-op — same policy as
    # b7c0d3e6f9a2.
    pass
