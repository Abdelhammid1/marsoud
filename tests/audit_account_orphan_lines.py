#!/usr/bin/env python3
"""MARSOUD-DB-FK-ENFORCEMENT-01 — no journal_lines may reference a
non-existent accounts.id, and the PRAGMA foreign_keys=ON that keeps
it that way must actually be engaged for SQLite connections.

A data review turned up real `journal_lines` rows whose `account_id`
had no matching row in `accounts` — the normal UI delete path
(`app/routes/accounts.py:301`) refuses to hard-delete an account with
lines, so those orphans were left behind by a raw-SQL / shell /
ad-hoc-script delete that bypassed the ORM guard.  Root cause: SQLite
defaults `PRAGMA foreign_keys=OFF`, so the FK on
`journal_lines.account_id -> accounts.id` (`ondelete=RESTRICT`) was
silently ignored.

Two checks:

  1. Diagnostic — sweep every `journal_lines` row in the current DB
     and confirm none reference a missing account.  The first time
     this runs on a prod DB it may fail; the printed IDs are the
     hand-cleanup list.
  2. Guard engaged — issue a raw `DELETE FROM accounts WHERE id=<id>`
     inside a rollback-only transaction, with a fresh company that
     has journal_lines against that account, and confirm the DB
     raises IntegrityError.  This proves
     `_marsoud_enable_sqlite_foreign_keys` in `app/__init__.py` is
     actually firing on every connection the pool hands out, not
     just declared.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


CHECKS = []
COMPANY_NAME = "__ACCOUNT_ORPHAN_LINES_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import Company, Account, JournalEntry, JournalLine
    from app.services.ledger import post_journal

    existing = Company.query.filter_by(name=COMPANY_NAME).first()
    if existing:
        _teardown_company(existing.id)

    orphan_ids = [r.id for r in JournalLine.query.filter(
        JournalLine.entry_id.notin_(db.session.query(JournalEntry.id))
    ).all()]
    if orphan_ids:
        JournalLine.query.filter(
            JournalLine.id.in_(orphan_ids)
        ).delete(synchronize_session=False)
        db.session.commit()

    c = Company(name=COMPANY_NAME, base_currency="EGP")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)

    cash = Account.query.filter_by(company_id=c.id, code="1110").one()
    payable = Account.query.filter_by(company_id=c.id, code="2210").one()

    post_journal(
        company_id=c.id,
        entry_date=date.today(),
        description="orphan-line audit fixture",
        lines=[
            {"account_id": cash.id,    "debit": 50.0, "credit": 0.0},
            {"account_id": payable.id, "debit": 0.0,  "credit": 50.0},
        ],
        source_type="rollup_audit_fixture",
        source_id=0,
    )
    db.session.commit()

    _STATE["company_id"] = c.id
    _STATE["cash_id"] = cash.id


def _teardown_company(company_id):
    from app.models import Company, JournalEntry, JournalLine
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    entry_ids = [r.id for r in JournalEntry.query.filter_by(
        company_id=company_id).all()]
    if entry_ids:
        JournalLine.query.filter(
            JournalLine.entry_id.in_(entry_ids)
        ).delete(synchronize_session=False)
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {c["name"] for c in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id)
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()


# ─── 1. Diagnostic — no orphan lines in the current DB ─────────────
@check("1. every journal_lines.account_id resolves to an accounts row")
def _():
    rows = db.session.execute(text(
        "SELECT jl.id, jl.account_id "
        "FROM journal_lines jl "
        "LEFT JOIN accounts a ON a.id = jl.account_id "
        "WHERE a.id IS NULL"
    )).fetchall()
    if rows:
        preview = ", ".join(f"line #{r.id}->acc {r.account_id}"
                             for r in rows[:5])
        raise AssertionError(
            f"{len(rows)} orphan journal_lines found "
            f"(first 5: {preview}) — hand-clean before deploy"
        )
    return "no orphan journal_lines"


# ─── 2. PRAGMA foreign_keys=ON is actually engaged ────────────────
@check("2. raw DELETE of an account with lines is refused by the DB")
def _():
    account_id = _STATE["cash_id"]
    dialect = db.engine.dialect.name
    if dialect != "sqlite":
        # PG / MySQL enforce FKs by default; this check is SQLite-
        # specific because the PRAGMA hook only matters there.
        return f"skipped (dialect={dialect} enforces FKs natively)"

    # Confirm the PRAGMA is on this connection.
    pragma = db.session.execute(
        text("PRAGMA foreign_keys")
    ).scalar()
    assert pragma == 1, (
        f"PRAGMA foreign_keys is {pragma} on the session's connection "
        "— the connect-event listener isn't firing")

    # Try the raw delete inside a savepoint so we don't corrupt the
    # fixture — an IntegrityError proves the FK RESTRICT engaged.
    try:
        db.session.execute(
            text("DELETE FROM accounts WHERE id = :aid"),
            {"aid": account_id},
        )
        db.session.flush()
    except IntegrityError as e:
        db.session.rollback()
        return f"refused with IntegrityError ({str(e.orig)[:50]}…)"
    else:
        db.session.rollback()
        raise AssertionError(
            "SQLite accepted a raw DELETE on an account with lines "
            "— PRAGMA foreign_keys must not be engaged"
        )


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
                    import traceback
                    traceback.print_exc()
        finally:
            try:
                if "company_id" in _STATE:
                    _teardown_company(_STATE["company_id"])
                    print(f"\n(cleaned up fixture company "
                          f"#{_STATE['company_id']})")
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
