"""GAP-1 — Rename inventory account code 1140 → 1300 across all companies

Revision ID: p4d1a8b6c5e7
Revises: o3c9f6d8e2a4
Create Date: 2026-06-11 23:30:00

The original ERP-01 ticket specified `1300` for the Inventory account.
Phase 1 used the existing seeded `1140` to avoid touching the COA. This
migration renames the code to match the ticket exactly. Account NAMES
(Arabic + English) are unchanged. All journal lines posted against the
account.id keep working — they reference the row by id, not by code.

Steps:
  1. For each company, find the account with code='1140'. If found AND
     no account with code='1300' exists yet for that company, rename
     the code to '1300'. Idempotent.

Down: revert 1300 → 1140 for accounts named "Inventory" (the safe path
for accidental rerun). Skipping non-matching rows.
"""
from alembic import op
import sqlalchemy as sa


revision = "p4d1a8b6c5e7"
down_revision = "o3c9f6d8e2a4"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    # Rename per company. Use IFNULL guard against the unlikely case of
    # an existing 1300 row (would happen only if someone manually added
    # one). In that case, skip the rename — the manual entry wins.
    conn.execute(sa.text("""
        UPDATE accounts
           SET code = '1300'
         WHERE code = '1140'
           AND company_id NOT IN (
               SELECT company_id FROM accounts WHERE code = '1300'
           )
    """))


def downgrade():
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE accounts
           SET code = '1140'
         WHERE code = '1300'
           AND name = 'Inventory'
           AND company_id NOT IN (
               SELECT company_id FROM accounts WHERE code = '1140'
           )
    """))
