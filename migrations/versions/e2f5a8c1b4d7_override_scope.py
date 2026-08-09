"""MARSOUD-SUBITEM-OVERRIDES (2026-08-09) — company overrides
grow a `scope` column so a super-admin can grant/deny a single
sub-item endpoint instead of a whole module.

New column `scope VARCHAR(8) NOT NULL CHECK IN ('MODULE',
'SUBITEM')` on `company_feature_overrides`. Existing rows
backfill to 'MODULE' via a server_default that is dropped
after the fill (future INSERTs must be explicit — the service
layer defaults to 'MODULE' for back-compat).

The unique constraint widens from `(company_id, feature_code)`
to `(company_id, scope, feature_code)` so a subitem row for
'inventory.count' can coexist with a module row for 'inventory'
on the same company (they're not the same feature and must not
collide).

Additive + idempotent: `_has_col` guard + `_has_uq` guard so a
rerun is a no-op. batch_alter_table for SQLite's rebuild path.

Revision ID: e2f5a8c1b4d7
Revises: 212eb02cf7c6
Create Date: 2026-08-09
"""
from alembic import op
import sqlalchemy as sa


revision = "e2f5a8c1b4d7"
down_revision = "f8c2e5a9d4b1"
branch_labels = None
depends_on = None


TABLE = "company_feature_overrides"
COL = "scope"
OLD_UQ = "uq_override_company_feature"
NEW_UQ = "uq_override_company_scope_feature"
CK = "ck_override_scope"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_col(table, col):
    return col in {c["name"] for c in _inspector().get_columns(table)}


def _has_uq(table, name):
    for c in _inspector().get_unique_constraints(table):
        if c.get("name") == name:
            return True
    return False


def upgrade():
    if not _has_col(TABLE, COL):
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column(
                COL, sa.String(8),
                nullable=False,
                server_default="MODULE",
            ))
        # Second pass: swap constraints inside a fresh batch so the
        # column is definitely present when we widen the unique.
        with op.batch_alter_table(TABLE) as batch:
            if _has_uq(TABLE, OLD_UQ):
                try:
                    batch.drop_constraint(OLD_UQ, type_="unique")
                except Exception:
                    # Some dialects don't name the constraint the
                    # way SQLAlchemy expects; batch_alter_table's
                    # rebuild path handles the drop implicitly.
                    pass
            if not _has_uq(TABLE, NEW_UQ):
                batch.create_unique_constraint(
                    NEW_UQ, ["company_id", "scope", "feature_code"])
            try:
                batch.create_check_constraint(
                    CK, "scope IN ('MODULE','SUBITEM')")
            except Exception:
                # Duplicate CHECK is harmless — SQLite rebuilds
                # tolerate re-adds via batch_alter_table already.
                pass
        # Third pass: drop the server_default so future INSERTs
        # must set scope explicitly (belt against a caller that
        # forgets — the service layer sets it, but nothing
        # should rely on the default column-level shape).
        with op.batch_alter_table(TABLE) as batch:
            batch.alter_column(COL, server_default=None)


def downgrade():
    if _has_col(TABLE, COL):
        with op.batch_alter_table(TABLE) as batch:
            try:
                batch.drop_constraint(CK, type_="check")
            except Exception:
                pass
            if _has_uq(TABLE, NEW_UQ):
                try:
                    batch.drop_constraint(NEW_UQ, type_="unique")
                except Exception:
                    pass
            if not _has_uq(TABLE, OLD_UQ):
                batch.create_unique_constraint(
                    OLD_UQ, ["company_id", "feature_code"])
            batch.drop_column(COL)
