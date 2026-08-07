"""MARSOUD-ASSET-DISPOSAL-01 (2026-08-07) — asset disposal columns
+ new CoA rows (5950 Loss / 4550 Gain) backfilled for existing
companies.

Six new columns on `fixed_assets`:
  disposal_date, disposal_reason (VARCHAR enum), disposal_note,
  disposal_proceeds, disposal_journal_entry_id (FK), disposed_by_id (FK)

Additive; nothing existing touched. Idempotent — reruns safe
via _has_column / _account_exists guards.

For CoA seed rows: every company that already has 5900 or 4000
parents gets 5950 and 4550 inserted as leaves. New companies get
them via `seed_default_coa`.

Revision ID: x6v9f8i2a4g9
Revises: v4t9d6g0y2e7
Create Date: 2026-08-07
"""
from alembic import op
import sqlalchemy as sa


revision = 'x6v9f8i2a4g9'
down_revision = 'v4t9d6g0y2e7'
branch_labels = None
depends_on = None


TABLE = "fixed_assets"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


def _has_column(table, col):
    if not _has_table(table):
        return False
    return col in {c["name"] for c in _inspector().get_columns(table)}


def _account_exists(bind, company_id, code):
    row = bind.execute(sa.text(
        "SELECT id FROM accounts WHERE company_id=:c AND code=:k"),
        {"c": company_id, "k": code}).fetchone()
    return row is not None


def _get_account_id(bind, company_id, code):
    row = bind.execute(sa.text(
        "SELECT id FROM accounts WHERE company_id=:c AND code=:k"),
        {"c": company_id, "k": code}).fetchone()
    return row[0] if row else None


def _insert_account(bind, *, company_id, code, name, name_ar,
                     type_, parent_id, is_postable, normal_side):
    """Insert with the same column set seed_coa.py expects — the
    Account model, not raw column names. Keeps the two seed paths
    (fresh company via seed_default_coa vs backfill here) in sync."""
    # accounts has no created_at column — pattern matches the
    # canonical seed_default_coa path which also relies on
    # SQLAlchemy defaults for is_active. Every column here is
    # explicitly present on the model.
    bind.execute(sa.text(
        "INSERT INTO accounts "
        "(company_id, code, name, name_ar, type, parent_id, "
        " is_postable, normal_side, is_active) "
        "VALUES (:c, :k, :n, :na, :t, :p, :ip, :ns, 1)"),
        {"c": company_id, "k": code, "n": name, "na": name_ar,
         "t": type_, "p": parent_id, "ip": is_postable,
         "ns": normal_side})


def upgrade():
    # ─── Columns on fixed_assets ─────────────────────────────
    # Named FK constraints so SQLite's batch_alter_table can
    # move them through the table-copy dance without "Constraint
    # must have a name" errors — same trap the cash-custody
    # migration hit.
    adds = []
    if not _has_column(TABLE, "disposal_date"):
        adds.append(("disposal_date", sa.Date, {}, None))
    if not _has_column(TABLE, "disposal_reason"):
        adds.append(("disposal_reason", sa.String(24), {}, None))
    if not _has_column(TABLE, "disposal_note"):
        adds.append(("disposal_note", sa.Text, {}, None))
    if not _has_column(TABLE, "disposal_proceeds"):
        adds.append(("disposal_proceeds", sa.Numeric(15, 2),
                      {"server_default": "0"}, None))
    if not _has_column(TABLE, "disposal_journal_entry_id"):
        adds.append(("disposal_journal_entry_id", sa.Integer, {},
                      ("journal_entries.id",
                       "fk_fixed_assets_disposal_entry")))
    if not _has_column(TABLE, "disposed_by_id"):
        adds.append(("disposed_by_id", sa.Integer, {},
                      ("users.id", "fk_fixed_assets_disposed_by")))

    if adds:
        with op.batch_alter_table(TABLE) as bop:
            for name, coltype, kwargs, fk in adds:
                if fk:
                    target, fk_name = fk
                    bop.add_column(sa.Column(
                        name, coltype,
                        sa.ForeignKey(target, name=fk_name),
                        nullable=True, **kwargs))
                else:
                    bop.add_column(sa.Column(
                        name, coltype, nullable=True, **kwargs))

    # ─── Backfill CoA rows 5950 + 4550 for existing companies ─
    bind = op.get_bind()
    company_ids = [r[0] for r in bind.execute(sa.text(
        "SELECT id FROM companies")).fetchall()]
    for cid in company_ids:
        # 5950 Loss — under existing 5900 parent, if present.
        if not _account_exists(bind, cid, "5950"):
            parent_id = _get_account_id(bind, cid, "5900")
            if parent_id is not None:
                _insert_account(
                    bind, company_id=cid, code="5950",
                    name="Loss on Disposal of Fixed Assets",
                    name_ar="خسائر بيع أصول ثابتة",
                    type_="EXPENSE", parent_id=parent_id,
                    is_postable=True, normal_side="DEBIT")
        # 4550 Gain — under existing 4000 parent (sibling of 4500).
        if not _account_exists(bind, cid, "4550"):
            parent_id = _get_account_id(bind, cid, "4000")
            if parent_id is not None:
                _insert_account(
                    bind, company_id=cid, code="4550",
                    name="Gain on Disposal of Fixed Assets",
                    name_ar="أرباح بيع أصول ثابتة",
                    type_="REVENUE", parent_id=parent_id,
                    is_postable=True, normal_side="CREDIT")


def downgrade():
    # Reverse column adds (don't touch the seed rows — they're
    # data, not schema, and a downgrade shouldn't nuke a
    # company's chart of accounts).
    for col in ("disposed_by_id", "disposal_journal_entry_id",
                "disposal_proceeds", "disposal_note",
                "disposal_reason", "disposal_date"):
        if _has_column(TABLE, col):
            with op.batch_alter_table(TABLE) as bop:
                bop.drop_column(col)
