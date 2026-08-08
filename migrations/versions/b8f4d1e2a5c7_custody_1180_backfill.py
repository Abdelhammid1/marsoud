"""MARSOUD-CUSTODY-BUGS-02 (2026-08-08) — backfill account 1180
«عهد نقدية تحت التسوية» into every existing tenant.

1180 was added to `seed_default_coa` on 2026-08-07 when the
cash-custody feature shipped, but nothing ever backfilled it into
companies whose COA was seeded before that date. Result: every
legacy tenant hits «الحساب الأب 1180 غير موجود» the first time
they open /custody/new.

This migration inserts the 1180 HEADER (is_postable=False) under
the existing 1100 «الأصول المتداولة» header, byte-identical to
what seed_coa.py:52-53 would create for a new company. Idempotent
via `_account_exists` guard; skips any tenant that already has
1180, or whose 1100 grandparent is somehow missing (ancient COA
that needs human review before it's safe to touch).

Companion to `app/services/subsidiary.py::_lazy_create_known_header`
which covers the runtime case (super-admin deletes 1180 later, or
mid-migration state). Both paths land in a byte-identical row.

Revision ID: b8f4d1e2a5c7
Revises: y7w0g9j3b5h0
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa


revision = "b8f4d1e2a5c7"
down_revision = "y7w0g9j3b5h0"
branch_labels = None
depends_on = None


# ─── idempotency helpers — verbatim from a2b5c9d3f6e0 ───────────────
def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name):
    return name in _inspector().get_table_names()


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
    bind.execute(sa.text(
        "INSERT INTO accounts "
        "(company_id, code, name, name_ar, type, parent_id, "
        " is_postable, normal_side, is_active) "
        "VALUES (:c, :k, :n, :na, :t, :p, :ip, :ns, 1)"),
        {"c": company_id, "k": code, "n": name, "na": name_ar,
         "t": type_, "p": parent_id, "ip": is_postable, "ns": normal_side})


def upgrade():
    bind = op.get_bind()
    if not _has_table("accounts") or not _has_table("companies"):
        return
    company_ids = [
        r[0] for r in bind.execute(sa.text(
            "SELECT id FROM companies")).fetchall()
    ]
    for cid in company_ids:
        if _account_exists(bind, cid, "1180"):
            continue
        parent_id = _get_account_id(bind, cid, "1100")
        if parent_id is None:
            # Ancient / mid-migration COA — skip. The runtime lazy-
            # create in subsidiary.py raises a clearer "1180 AND 1100
            # missing" message if a user hits this tenant later.
            continue
        _insert_account(
            bind, company_id=cid, code="1180",
            name="Cash Custody in Settlement",
            name_ar="عهد نقدية تحت التسوية",
            type_="ASSET", parent_id=parent_id,
            is_postable=False,      # HEADER
            normal_side="DEBIT")


def downgrade():
    # No-op. CoA rows are DATA (a company's chart), not schema — a
    # downgrade shouldn't nuke them. Same convention
    # x6v9f8i2a4g9_asset_disposal and a2b5c9d3f6e0_ops_hub_expansion
    # follow.
    pass
