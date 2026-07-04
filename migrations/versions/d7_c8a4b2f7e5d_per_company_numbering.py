"""PER-CO-NUMBERING (Abdelhamid 2026-07-04) — Lead + Project counts
from 1 per company.

Adds:
  - leads.number
  - projects.number
  - Backfill: every existing row in each company gets a per-company
    sequential number (L-0001, PRJ-0001, ...) in created_at order.
  - Advances the corresponding NumberSequence so future next_number()
    calls pick up from the last backfilled value.

Revision ID: d7_c8a4b2f7e5d
Revises: d6_e3f9a2b7c8d
"""
from alembic import op
import sqlalchemy as sa


revision = "d7_c8a4b2f7e5d"
down_revision = "d6_e3f9a2b7c8d"
branch_labels = None
depends_on = None


def _has_col(table, col):
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return col in {c["name"] for c in insp.get_columns(table)}


# Match app/services/numbering.py.
_PREFIXES = {"LEAD": "L", "PROJECT": "PRJ"}


def upgrade():
    if not _has_col("leads", "number"):
        with op.batch_alter_table("leads") as batch:
            batch.add_column(sa.Column("number", sa.String(30),
                                          nullable=True, index=True))
    if not _has_col("projects", "number"):
        with op.batch_alter_table("projects") as batch:
            batch.add_column(sa.Column("number", sa.String(30),
                                          nullable=True, index=True))

    bind = op.get_bind()

    # Backfill leads per company in created_at order.
    for row in bind.execute(sa.text(
        "SELECT DISTINCT company_id FROM leads",
    )).fetchall():
        cid = row[0]
        n = 0
        for lid in [r[0] for r in bind.execute(sa.text(
            "SELECT id FROM leads WHERE company_id = :c "
            "ORDER BY created_at ASC, id ASC",
        ), {"c": cid}).fetchall()]:
            n += 1
            bind.execute(sa.text(
                "UPDATE leads SET number = :num WHERE id = :id",
            ), {"num": f"L-{n:04d}", "id": lid})
        _advance_sequence(bind, cid, "LEAD", "L", n)

    # Backfill projects per company in created_at order.
    for row in bind.execute(sa.text(
        "SELECT DISTINCT company_id FROM projects",
    )).fetchall():
        cid = row[0]
        n = 0
        for pid in [r[0] for r in bind.execute(sa.text(
            "SELECT id FROM projects WHERE company_id = :c "
            "ORDER BY created_at ASC, id ASC",
        ), {"c": cid}).fetchall()]:
            n += 1
            bind.execute(sa.text(
                "UPDATE projects SET number = :num WHERE id = :id",
            ), {"num": f"PRJ-{n:04d}", "id": pid})
        _advance_sequence(bind, cid, "PROJECT", "PRJ", n)


def _advance_sequence(bind, company_id, doc_type, prefix, up_to):
    """After the backfill, set the NumberSequence's next_number to
    up_to+1 so the next runtime next_number() call doesn't collide
    with a backfilled row.

    Silent no-op if the number_sequences table doesn't exist on this
    DB (old migrations state)."""
    insp = sa.inspect(bind)
    if "number_sequences" not in insp.get_table_names():
        return
    existing = bind.execute(sa.text(
        "SELECT id, next_number FROM number_sequences "
        "WHERE company_id = :c AND doc_type = :d",
    ), {"c": company_id, "d": doc_type}).fetchone()
    target = up_to + 1
    if existing:
        seq_id, cur = existing[0], existing[1]
        if cur < target:
            bind.execute(sa.text(
                "UPDATE number_sequences SET next_number = :n "
                "WHERE id = :i",
            ), {"n": target, "i": seq_id})
    else:
        bind.execute(sa.text(
            "INSERT INTO number_sequences "
            "(company_id, doc_type, next_number) "
            "VALUES (:c, :d, :n)",
        ), {"c": company_id, "d": doc_type, "n": target})


def downgrade():
    if _has_col("projects", "number"):
        with op.batch_alter_table("projects") as batch:
            batch.drop_column("number")
    if _has_col("leads", "number"):
        with op.batch_alter_table("leads") as batch:
            batch.drop_column("number")
