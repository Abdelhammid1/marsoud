"""MARSOUD-CRM-NO-RESPONSE + MARSOUD-LEAD-AUTOCONTACT (2026-07-13).

Combined migration for two related tickets:

  · NO_RESPONSE stage — a parking status for leads that never
    replied after multiple contact attempts. Added to the LeadStatus
    enum WITHOUT collapsing it into LOST.

  · Every Lead gets a primary Contact on creation. For existing
    Leads with no LeadContact row, backfill one from the lead's own
    name + phone. Idempotent — reruns are safe.

The enum change is only a schema step on Postgres (native ENUM
type). SQLite stores enums as VARCHAR so no DDL is needed.

Revision ID: b7c0d3e6f9a2
Revises: a6b9c2d5e8f1
Create Date: 2026-07-13
"""
from alembic import op
import sqlalchemy as sa


revision = 'b7c0d3e6f9a2'
down_revision = 'a6b9c2d5e8f1'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name

    # ─── Enum expansion — Postgres only. ────────────────────────────
    # `db.Enum(LeadStatus)` creates a real ENUM type on Postgres, so
    # a new value needs an explicit ALTER TYPE. SQLite (dev) stores
    # enums as plain VARCHAR — no schema change is needed there.
    if dialect == "postgresql":
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction
        # block on older Postgres versions, so we execute it with
        # AUTOCOMMIT semantics via a separate connection.
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE leadstatus ADD VALUE IF NOT EXISTS 'NO_RESPONSE'"
            )

    # ─── Backfill primary contacts for legacy leads. ───────────────
    # Rule (from ticket): for every Lead that has ZERO rows in
    # lead_contacts, insert one row copying (name, phone) from the
    # Lead itself. Deduped via the NOT EXISTS subquery so a rerun
    # does nothing.
    # Truthy literal + timestamp are dialect-portable when written
    # as raw SQL (CURRENT_TIMESTAMP is standard SQL and both SQLite
    # and Postgres honour it).
    op.execute(sa.text("""
        INSERT INTO lead_contacts
            (company_id, lead_id, name, phone, is_primary, created_at)
        SELECT
            l.company_id, l.id, l.client_name, l.phone,
            1, CURRENT_TIMESTAMP
        FROM leads l
        WHERE l.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM lead_contacts c
              WHERE c.lead_id = l.id
          )
    """))


def downgrade():
    # Enum values can't be removed cleanly on Postgres; leaving them
    # is safe (no data uses them post-downgrade because the model
    # rejects them). Backfilled contacts stay too — deleting them
    # would be destructive and the ticket explicitly says a contact
    # deletion should never cascade to the lead.
    pass
