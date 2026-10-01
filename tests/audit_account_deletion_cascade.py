#!/usr/bin/env python3
"""MARSOUD-ACCOUNT-DELETION-CASCADE-AUDIT-01 (2026-09-30, pushed red
on 2026-10-01) — the "sole owner of a company with no other members"
branch of `app/services/account_deletion.py::delete_account()` under
`PRAGMA foreign_keys=ON`.

This audit is pushed RED on purpose, per Abdelhamid's instruction
("إخفاؤه مش حل" — hiding it is not the fix).  Check 1 currently fails
and the deploy gate correctly refuses to ship account_deletion changes
until the full-cascade follow-up ticket lands.

Current status:
  * Check 1: FAIL — delete_account() raises IntegrityError on
    `DELETE FROM users` because `platform_audit_logs.actor_id` still
    references the user being deleted.  `post_journal()` writes a
    PlatformAuditLog row for every JE it posts
    (`app/services/lifecycle.py`), that table has no `company_id`
    column so the sorted_tables sweep in step 1 doesn't touch it, and
    `_blank_or_delete_user_refs` doesn't manage to null the ref in
    time before `db.session.delete(user)` runs at step 4.
  * Checks 2-5: cascade (either the row survives rollback or the
    expected cascade didn't happen).

Partial fix already landed in `b52268d` — pre-deletes `journal_lines`
by `entry_id` before the sorted_tables loop reaches `journal_entries`.
That removed the first FK trip; the second (platform_audit_logs) is
documented inline in `account_deletion.py` and tracked as the
follow-up.

Fix path (follow-up ticket):
  * Order `_blank_or_delete_user_refs` BEFORE the sorted_tables
    sweep, OR
  * Walk every FK-to-users table under PRAGMA enforcement and null /
    delete the refs explicitly, including `platform_audit_logs`
    (`actor_id` + `target_user_id`) which currently escape the
    tenant-scoped sweep because they have no `company_id`.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
COMPANY_NAME = "__ACCOUNT_DELETION_CASCADE_AUDIT__"
USER_EMAIL = "__acct_delete_cascade__@audit.local"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    from app.models import (
        Company, User, Account, JournalEntry, JournalLine,
    )
    from app.models.user import user_companies
    from app.services.ledger import post_journal
    from sqlalchemy import text

    # PRAGMA=OFF for leftover cleanup so orphan sweeps don't trip FKs.
    db.session.execute(text("PRAGMA foreign_keys=OFF"))

    # Nuke any leftover from prior aborted runs.
    for u in User.query.filter_by(email=USER_EMAIL).all():
        db.session.execute(user_companies.delete().where(
            user_companies.c.user_id == u.id))
        db.session.delete(u)
    for c in Company.query.filter_by(name=COMPANY_NAME).all():
        _teardown_company_hard(c.id)
    db.session.commit()

    # Sweep orphans — SQLite reuses user + company IDs across runs,
    # so an old row that referenced a now-reused id would silently
    # attach to our fresh fixture and skew check-4.
    orphan_ids = [r.id for r in JournalLine.query.filter(
        JournalLine.entry_id.notin_(db.session.query(JournalEntry.id))
    ).all()]
    if orphan_ids:
        JournalLine.query.filter(
            JournalLine.id.in_(orphan_ids)
        ).delete(synchronize_session=False)
    for tbl, col, parent in [
        ("platform_audit_logs",  "actor_id",         "users"),
        ("platform_audit_logs",  "target_user_id",   "users"),
        ("platform_audit_logs",  "target_company_id","companies"),
        ("user_sessions",        "user_id",          "users"),
        ("user_activity_log",    "user_id",          "users"),
        ("api_tokens",           "user_id",          "users"),
        ("user_companies",       "user_id",          "users"),
        ("user_companies",       "company_id",       "companies"),
    ]:
        try:
            db.session.execute(text(
                f"DELETE FROM {tbl} "
                f"WHERE {col} IS NOT NULL "
                f"AND {col} NOT IN (SELECT id FROM {parent})"
            ))
        except Exception:  # noqa: BLE001
            pass
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()

    # Fresh fixture — company + sole-owner user + one posted JE.
    c = Company(name=COMPANY_NAME, base_currency="EGP")
    db.session.add(c); db.session.flush()
    from app.services.seed_coa import seed_default_coa
    seed_default_coa(c.id)

    u = User(email=USER_EMAIL, full_name="Delete Cascade Audit",
              password_hash="x")
    db.session.add(u); db.session.flush()

    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))

    cash = Account.query.filter_by(company_id=c.id, code="1110").one()
    payable = Account.query.filter_by(company_id=c.id, code="2210").one()
    post_journal(
        company_id=c.id,
        entry_date=date.today(),
        description="cascade fixture",
        lines=[
            {"account_id": cash.id,    "debit": 100.0, "credit": 0.0},
            {"account_id": payable.id, "debit": 0.0,   "credit": 100.0},
        ],
        source_type=None,
        source_id=None,
        created_by=u.id,
    )
    db.session.commit()

    _STATE["company_id"] = c.id
    _STATE["user_id"] = u.id


def _teardown_company_hard(company_id):
    """Scratch-company nuke used ONLY from _setup to clean up
    leftovers from prior aborted runs.  Never called from the test
    body — the test body is what we're auditing."""
    from app.models import Company, JournalEntry, JournalLine
    from sqlalchemy import inspect, text
    db.session.rollback()
    db.session.execute(text("PRAGMA foreign_keys=OFF"))
    insp = inspect(db.engine)
    entry_ids = [r.id for r in JournalEntry.query.filter_by(
        company_id=company_id).all()]
    if entry_ids:
        JournalLine.query.filter(
            JournalLine.entry_id.in_(entry_ids)
        ).delete(synchronize_session=False)
    for table in reversed(db.metadata.sorted_tables):
        if "company_id" in {col["name"] for col in insp.get_columns(table.name)}:
            db.session.execute(
                table.delete().where(table.c.company_id == company_id)
            )
    c = db.session.get(Company, company_id)
    if c:
        db.session.delete(c)
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    db.session.commit()


# ─── 1. delete_account() completes without IntegrityError ─────────
@check("1. delete_account() returns success under PRAGMA foreign_keys=ON")
def _():
    from app.models import User
    from app.services.account_deletion import delete_account
    user = db.session.get(User, _STATE["user_id"])
    result = delete_account(user)
    assert result is not None, "delete_account returned None"
    assert result.get("companies_deleted") == 1, \
        f"expected 1 company deleted, got {result.get('companies_deleted')}"
    return f"companies_deleted={result['companies_deleted']}"


# ─── 2-5. Rows actually gone ─────────────────────────────────────
@check("2. Company row is gone")
def _():
    from app.models import Company
    assert db.session.get(Company, _STATE["company_id"]) is None, \
        "Company row survived delete_account"
    return "gone"


@check("3. No JournalEntry left for the deleted company")
def _():
    from app.models import JournalEntry
    n = JournalEntry.query.filter_by(
        company_id=_STATE["company_id"]).count()
    assert n == 0, f"{n} JournalEntry rows survived cascade"
    return "0 rows"


@check("4. No orphan JournalLine left behind (invariant from T2)")
def _():
    from sqlalchemy import text
    orphans = db.session.execute(text(
        "SELECT jl.id FROM journal_lines jl "
        "LEFT JOIN journal_entries je ON je.id = jl.entry_id "
        "WHERE je.id IS NULL"
    )).fetchall()
    assert not orphans, f"{len(orphans)} orphan journal_lines"
    return "0 orphan lines"


@check("5. User row is gone")
def _():
    from app.models import User
    assert db.session.get(User, _STATE["user_id"]) is None, \
        "User row survived delete_account"
    return "gone"


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
        finally:
            # If delete_account somehow left the fixture rows behind,
            # still wipe them so the next audit run isn't polluted.
            try:
                if "company_id" in _STATE:
                    from app.models import Company
                    if db.session.get(Company, _STATE["company_id"]):
                        _teardown_company_hard(_STATE["company_id"])
            except Exception as e:  # noqa: BLE001
                print(f"\n(teardown failed: {e})")
    print()
    print(f"----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
