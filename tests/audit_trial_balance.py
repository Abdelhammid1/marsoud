#!/usr/bin/env python3
"""MARSOUD-TKT-TRIAL-BALANCE (2026-09-02) — ميزان المراجعة.

New financial report: every account, total debit, total credit, signed
balance for the period, with a bottom Σ that MUST equal (Σd = Σc).
Reuses the balance-sheet / income-statement pattern for gating, PDF +
Excel export, permissions, and paused-journal filtering.

Checks (matching the ticket's acceptance criteria):
  1. Service `trial_balance_report` exists with the right signature.
  2. Full-range (no dates): every account with movement shows up,
     totals.debit == totals.credit, totals.balanced is True.
  3. Range-narrowed: filtering to a shorter window returns numbers
     strictly ≤ the full-range totals (AC #2).
  4. Rows with zero debit + zero credit AND no children are hidden (AC #5).
  5. A parent account with children stays visible even when its own
     direct lines sum to zero (AC #6).
  6. `is_active=False` (paused) journal entries are excluded (AC #7).
  7. Route: GET /reports/trial-balance renders 200 with the totals row
     and no red banner when the ledger balances.
  8. Export dispatch: /reports/trial-balance/export/pdf and /export/excel
     both return non-empty bodies with the right content-type (AC #8/#9).
  9. Reports index card renders the ميزان المراجعة link.
"""
import os
import sys
from pathlib import Path
from datetime import datetime, date, timedelta
from decimal import Decimal

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _boot(prefix):
    """Company + owner user with reports.view + a seeded ledger with
    one balanced journal entry (Dr 1110 / Cr 4110 100.00)."""
    from sqlalchemy import text, inspect
    from app import db
    from app.models import (
        Company, User, Plan, Account, JournalEntry, JournalLine,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

    insp = inspect(db.engine)
    # Sweep every prior TB* prefix, not just the current — SQLite reuses
    # freed rowids, so orphan journal_lines from a prior TB6 run can
    # attach to a freshly-inserted account with the same id in this run
    # and inflate my trial balance totals. Clean them all up.
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE '__TB%'"))]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"),
                    {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        "DELETE FROM users WHERE email LIKE '%__tb%'"))
    # Kill orphan journal_entries whose company was deleted long ago
    # (accumulated from every audit that ever ran against this DB), plus
    # journal_lines whose parent entry no longer exists AND lines
    # pointing at accounts whose company was deleted. SQLite reuses
    # rowids, so a freshly-inserted account with id N can collide with
    # an old orphan line's account_id=N and inflate the totals.
    db.session.execute(text(
        "DELETE FROM journal_entries WHERE company_id NOT IN (SELECT id FROM companies)"))
    db.session.execute(text(
        "DELETE FROM journal_lines WHERE entry_id NOT IN (SELECT id FROM journal_entries)"))
    db.session.execute(text(
        "DELETE FROM journal_lines WHERE account_id NOT IN (SELECT id FROM accounts)"))
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C",
                    allowed_subitems=None)
        db.session.add(plan)
    plan.set_modules(["accounting", "sales", "hr", "reports"])
    db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"

    owner = User(email=f"owner__{prefix.lower()}__@x.io",
                 full_name=f"Owner {prefix}", is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv, terms_accepted_at=datetime.utcnow())
    owner.set_password("pw12345678")
    db.session.add(owner); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))
    db.session.commit()

    return owner.email, c.id, owner.id


def _teardown(prefix):
    from sqlalchemy import text, inspect
    from app import db
    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        f"SELECT id FROM companies WHERE name LIKE '__{prefix}__%'"))]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"),
                    {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        f"DELETE FROM users WHERE email LIKE '%__{prefix.lower()}__%'"))
    db.session.commit()


def _authed_client(app, oid, cid):
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(oid)
        s["_fresh"] = True
        s["active_company_id"] = cid
    return c


def _post_je(cid, *, when=None, debit_code="1110", credit_code="4100",
             amount=100, active=True, source="test"):
    """Post one balanced journal entry Dr <debit_code> / Cr <credit_code>
    for the given amount. Returns the JournalEntry."""
    from app import db
    from app.models import Account, JournalEntry, JournalLine
    when = when or date.today()
    da = Account.query.filter_by(company_id=cid, code=debit_code).first()
    ca = Account.query.filter_by(company_id=cid, code=credit_code).first()
    assert da is not None, f"no account {debit_code}"
    assert ca is not None, f"no account {credit_code}"
    je = JournalEntry(company_id=cid, date=when,
                      description=f"audit-{source}",
                      source_type=source, is_active=active)
    db.session.add(je); db.session.flush()
    db.session.add(JournalLine(entry_id=je.id, account_id=da.id,
                                debit=Decimal(str(amount)),
                                credit=Decimal(0),
                                debit_base=Decimal(str(amount)),
                                credit_base=Decimal(0)))
    db.session.add(JournalLine(entry_id=je.id, account_id=ca.id,
                                debit=Decimal(0),
                                credit=Decimal(str(amount)),
                                debit_base=Decimal(0),
                                credit_base=Decimal(str(amount))))
    db.session.commit()
    return je


@check("1. trial_balance_report signature")
def _():
    import inspect as _inspect
    from app.services.reports import trial_balance_report
    sig = _inspect.signature(trial_balance_report)
    params = list(sig.parameters)
    assert params[0] == "company_id"
    assert "start_date" in sig.parameters
    assert "end_date" in sig.parameters
    return "signature ok"


@check("2. full-range: Σd = Σc, balanced=True")
def _():
    from app import create_app
    from app.services.reports import trial_balance_report

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TB2")
        try:
            _post_je(cid, amount=250)
            data = trial_balance_report(cid)
            assert data["totals"]["debit"] == 250.0
            assert data["totals"]["credit"] == 250.0
            assert data["totals"]["balanced"] is True
            # Both touched accounts appear
            codes = {r["code"] for r in data["rows"]}
            assert "1110" in codes and "4100" in codes
            return f"balanced, {len(data['rows'])} rows visible"
        finally:
            _teardown("TB2")


@check("3. narrow range → smaller totals than full range")
def _():
    from app import create_app
    from app.services.reports import trial_balance_report

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TB3")
        try:
            today = date.today()
            _post_je(cid, amount=100, when=today - timedelta(days=90))
            _post_je(cid, amount=200, when=today)
            full = trial_balance_report(cid)
            narrow = trial_balance_report(
                cid, start_date=today - timedelta(days=7), end_date=today)
            assert full["totals"]["debit"] == 300.0, full["totals"]
            assert narrow["totals"]["debit"] == 200.0, narrow["totals"]
            assert narrow["totals"]["debit"] < full["totals"]["debit"]
            return "narrow < full (200 < 300)"
        finally:
            _teardown("TB3")


@check("4. zero-movement leaf accounts are hidden")
def _():
    from app import create_app
    from app.models import Account
    from app.services.reports import trial_balance_report

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TB4")
        try:
            # No JE posted → COA has many accounts but none have movement
            data = trial_balance_report(cid)
            # A leaf account (say 5310 utilities) with no movement + no
            # children must be hidden.
            hidden = [r for r in data["rows"] if r["code"] == "5310"]
            assert hidden == [], \
                f"leaf 5310 should be hidden with no movement, got: {hidden}"
            return f"leaf-with-zero hidden ({len(data['rows'])} rows shown)"
        finally:
            _teardown("TB4")


@check("5. parent account with children stays visible")
def _():
    from app import create_app
    from app.models import Account
    from app.services.reports import trial_balance_report

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TB5")
        try:
            # Find any parent-with-children in the default COA
            parents = (Account.query
                       .filter_by(company_id=cid, is_active=True)
                       .all())
            with_kids = [a for a in parents if a.children]
            assert with_kids, "seed COA should have at least one parent"
            data = trial_balance_report(cid)
            codes = {r["code"] for r in data["rows"]}
            found = [a for a in with_kids if a.code in codes]
            assert found, (
                "no parent-with-children was rendered — hierarchy "
                "would collapse in the UI"
            )
            return f"{len(found)}/{len(with_kids)} parents kept"
        finally:
            _teardown("TB5")


@check("6. paused (is_active=False) journal entries excluded")
def _():
    from app import create_app
    from app.services.reports import trial_balance_report

    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TB6")
        try:
            _post_je(cid, amount=100, active=True)
            _post_je(cid, amount=500, active=False)  # paused, must be excluded
            data = trial_balance_report(cid)
            assert data["totals"]["debit"] == 100.0, (
                f"paused JE leaked into totals: {data['totals']}"
            )
            assert data["totals"]["credit"] == 100.0
            return "paused JE correctly excluded"
        finally:
            _teardown("TB6")


@check("7. GET /reports/trial-balance renders 200 with no banner when balanced")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TB7")
        try:
            _post_je(cid, amount=333)
            client = _authed_client(app, oid, cid)
            r = client.get("/reports/trial-balance")
            assert r.status_code == 200, (
                f"got {r.status_code} → {r.headers.get('Location')}")
            html = r.data.decode("utf-8")
            assert "ميزان المراجعة" in html
            assert "الإجمالي" in html
            # No red banner when balanced
            assert "لا يساوي مجموع الدائن" not in html, \
                "balanced ledger should not raise the red banner"
            return "route renders 200 with totals row"
        finally:
            _teardown("TB7")


@check("8. export dispatch: PDF + Excel non-empty, correct MIME")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TB8")
        try:
            _post_je(cid, amount=777)
            client = _authed_client(app, oid, cid)
            # Excel first — always available (no libpango dep)
            r = client.get("/reports/trial-balance/export/excel")
            assert r.status_code == 200, f"excel got {r.status_code}"
            assert "spreadsheetml" in r.headers.get("Content-Type", "")
            assert len(r.data) > 500, "excel body suspiciously small"
            # PDF — WeasyPrint on Windows may fall back to ReportLab.
            # Either way the body must be a real PDF.
            r = client.get("/reports/trial-balance/export/pdf")
            assert r.status_code == 200, f"pdf got {r.status_code}"
            assert r.headers.get("Content-Type") == "application/pdf"
            assert r.data.startswith(b"%PDF"), "not a PDF body"
            return "excel + pdf both ship"
        finally:
            _teardown("TB8")


@check("9. reports index shows the ميزان المراجعة card")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("TB9")
        try:
            client = _authed_client(app, oid, cid)
            r = client.get("/reports/")
            assert r.status_code == 200
            html = r.data.decode("utf-8")
            assert "ميزان المراجعة" in html, \
                "index missing the new report card"
            assert "/reports/trial-balance" in html
            return "card + link both present"
        finally:
            _teardown("TB9")


def main():
    passed = failed = 0
    for label, fn in CHECKS:
        try:
            res = fn()
            print(f"PASS  {label}  ⇒ {res}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
            failed += 1
            import traceback; traceback.print_exc()
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
