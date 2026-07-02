"""Preflight orphan sweep — call once at the start of an audit's
main() to clean up pollution left by prior audit runs. Idempotent.

Every audit script calls its own _teardown_company at the end, but
those teardowns miss grandchild rows keyed on non-company_id FKs
(invoice_items, bom_lines, journal_lines, etc.). This helper wipes
every such orphan across the DB before the current audit builds
its fixture, guaranteeing a clean slate.
"""
from sqlalchemy import text, inspect

from app import db


def preflight():
    from tests._teardown import _sweep_orphans
    insp = inspect(db.engine)
    live = set(insp.get_table_names())
    with db.engine.begin() as conn:
        _sweep_orphans(conn, live)
