"""MARSOUD-53 — backfill default warehouse for companies that lack one

For every active company that has zero Warehouse rows, insert a default
MAIN warehouse so the inventory flows (product creation with opening
balance, vendor bill INVENTORY lines, POS) don't error with the
ambiguous "المخزن غير صحيح" message.

The original Phase 1 inventory migration seeded MAIN for companies that
existed at that migration's runtime, but companies created BEFORE the
inventory module landed (or accidentally cleaned warehouses) end up
without one. This is an idempotent re-seed.

Revision ID: v1c4d7a2b8e5
Revises: u9b6e3d2a8f4
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime

revision = 'v1c4d7a2b8e5'
down_revision = 'u9b6e3d2a8f4'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    # Find every active company that has NO warehouse rows.
    rows = conn.execute(sa.text(
        "SELECT c.id FROM companies c "
        "WHERE c.is_active = 1 "
        "AND NOT EXISTS (SELECT 1 FROM warehouses w WHERE w.company_id = c.id)"
    )).fetchall()
    now = datetime.utcnow().isoformat(sep=" ", timespec="seconds")
    inserted = 0
    for (cid,) in rows:
        conn.execute(sa.text(
            "INSERT INTO warehouses (company_id, code, name, is_default, "
            "is_active, created_at) VALUES (:cid, 'MAIN', :name, 1, 1, :now)"
        ), {"cid": cid, "name": "المخزن الرئيسي", "now": now})
        inserted += 1
    print(f"  → backfilled MAIN warehouse for {inserted} companies")


def downgrade():
    # No-op — we don't know which MAIN warehouses came from this backfill vs
    # the original Phase 1 seed. Dropping them all would lose real data.
    pass
