#!/usr/bin/env python3
"""MARSOUD-ACCOUNTING-OPS + MARSOUD-CURRENCY-AR — audit.

Covers two tickets:

Accounting operations (🧮 العمليات المحاسبية)
  1. The page is registry-driven: every OPERATIONS entry gets a card, and
     the index has no hardcoded operation names.
  2. Each wizard posts a BALANCED entry hitting the right two accounts:
        capital        Dr cash/bank  · Cr 3100
        opening-balance Dr cash/bank · Cr 3900
        owner-drawings Dr 3200       · Cr cash/bank
  3. Every wizard stamps its own source_type and it resolves to a real
     label (not "قيد يدوي").
  4. A missing equity account raises a clean error, not a 500.
  5. An unknown op_key redirects to the index instead of blowing up.
  6. The user is never asked for a debit/credit account — the form has no
     account picker at all.

Currency
  7. currency_ar maps every supported code and falls back to the raw code.
  8. No template prints a raw currency code any more.
  9. The journal form preselects the COMPANY's currency, not SAR.
 10. post_journal() with no currency inherits the company's, not "SAR".
"""
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
COMPANY_NAME = "__ACCT_OPS_AUDIT__"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _setup():
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.roles import set_membership_role
    from app.services.legal import get_terms_version
    from app.services.plan_gating import plan_allows
    from datetime import datetime

    _teardown()

    # base_currency EGP on purpose — it is what catches a SAR leak.
    co = Company(name=COMPANY_NAME, base_currency="EGP", vat_rate=0)
    db.session.add(co)
    db.session.flush()
    for pl in Plan.query.order_by(Plan.id).all():
        co.plan_id = pl.id
        co.intended_plan_id = pl.id
        db.session.flush()
        if plan_allows("journals.create", co):
            break
    db.session.commit()

    seed_default_coa(co.id)
    ensure_roles_ready_for_company(co.id)

    u = User(email="__acctops@audit.local", full_name="AcctOps Owner",
             is_active=True, terms_version=get_terms_version(),
             terms_accepted_at=datetime.utcnow())
    u.set_password("Passw0rd!audit1")
    db.session.add(u)
    db.session.flush()
    db.session.commit()
    set_membership_role(u.id, co.id, "owner")

    _STATE["cid"] = co.id
    _STATE["uid"] = u.id


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text
    co = Company.query.filter_by(name=COMPANY_NAME).first()
    if co:
        cid = co.id
        for tbl in ("journal_lines",):
            db.session.execute(text(
                "DELETE FROM journal_lines WHERE entry_id IN "
                "(SELECT id FROM journal_entries WHERE company_id=:c)"),
                {"c": cid})
        for tbl in ("journal_entries", "payment_methods", "accounts",
                    "user_companies", "role_permissions"):
            try:
                if tbl == "role_permissions":
                    db.session.execute(text(
                        "DELETE FROM role_permissions WHERE role_id IN "
                        "(SELECT id FROM roles WHERE company_id=:c)"),
                        {"c": cid})
                else:
                    db.session.execute(
                        text(f"DELETE FROM {tbl} WHERE company_id=:c"),
                        {"c": cid})
            except Exception:
                db.session.rollback()
        db.session.execute(text("DELETE FROM roles WHERE company_id=:c"),
                           {"c": cid})
        db.session.execute(text("DELETE FROM companies WHERE id=:c"),
                           {"c": cid})
        db.session.commit()
    u = User.query.filter_by(email="__acctops@audit.local").first()
    if u:
        db.session.delete(u)
        db.session.commit()


def _client():
    app = _STATE["app"]
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(_STATE["uid"])
        s["_fresh"] = True
        s["active_company_id"] = _STATE["cid"]
    return c


def _acc(code):
    from app.services.ledger import get_account_by_code
    return get_account_by_code(_STATE["cid"], code)


# ─── Accounting operations ──────────────────────────────────────────────
@check("1. index page is driven by the registry (a card per operation)")
def _():
    from app.services.accounting_ops import OPERATIONS
    body = _client().get("/accounting-ops/").get_data(as_text=True)
    for op in OPERATIONS:
        assert op.title in body, f"card missing for {op.key}"
        assert f"/accounting-ops/{op.key}" in body, f"link missing for {op.key}"
    tpl = (ROOT / "app/templates/accounting_ops/index.html").read_text(
        encoding="utf-8")
    for op in OPERATIONS:
        assert op.title not in tpl, (
            f"'{op.title}' is hardcoded in the template — the page must be "
            "registry-driven so new operations need no template edit")
    return f"{len(OPERATIONS)} cards, none hardcoded"


@check("2. each wizard posts a balanced entry on the right accounts")
def _():
    from app.models import JournalEntry
    from app.services.accounting_ops import get_operation, run_operation
    from app.services.ledger import postable_under
    cid = _STATE["cid"]
    cash, bank = str(_acc("1110").id), str(_acc("1121").id)
    # MARSOUD-FINANCIAL-ACCOUNT-FIELD — the wizards take the account
    # itself, not a payment method, and it is required.
    money = {"account_id": cash}
    expense = postable_under(cid, "5000")[0]

    # MARSOUD-OPS-FOUNDATION — every registered operation is exercised,
    # not a hardcoded three. `extra` carries whatever pickers the
    # operation adds beyond the money account; ORDER MATTERS, the settle
    # wizard needs the accrual that precedes it to exist.
    expected = [
        ("capital",                ("1110", "3100"), money),
        ("opening-balance",        ("1110", "3900"), money),
        ("owner-drawings",         ("3200", "1110"), money),
        ("transfer",               ("1121", "1110"),
         {"account_id": cash, "account_id_to": bank}),
        ("accrue-expense",         (expense.code, "2160"),
         {"expense_account_id": str(expense.id)}),
        ("settle-accrued-expense", ("2160", "1110"), None),
    ]
    out = []
    for key, (dr_code, cr_code), extra in expected:
        op = get_operation(key)
        if extra is None:                       # settle the accrual above
            from app.services.open_items import open_items_for
            items = open_items_for(cid, kind="accrued_expense")
            assert items, "the accrual wizard left nothing to settle"
            extra = {"account_id": cash, "open_item_id": str(items[-1].id)}
        entry = run_operation(op, cid, dict(
            extra, amount="500", date=date.today().isoformat(),
            notes="audit"), actor_id=_STATE["uid"])
        e = db.session.get(JournalEntry, entry.id)
        d = sum(float(l.debit) for l in e.lines)
        c = sum(float(l.credit) for l in e.lines)
        assert abs(d - c) < 0.005, f"{key}: unbalanced {d} vs {c}"
        assert abs(d - 500) < 0.005, f"{key}: total {d} != 500"
        dr = [l for l in e.lines if float(l.debit) > 0]
        cr = [l for l in e.lines if float(l.credit) > 0]
        assert len(dr) == 1 and len(cr) == 1, f"{key}: expected 2 lines"
        assert dr[0].account_id == _acc(dr_code).id, (
            f"{key}: debit should hit {dr_code}")
        assert cr[0].account_id == _acc(cr_code).id, (
            f"{key}: credit should hit {cr_code}")
        out.append(f"{key} Dr{dr_code}/Cr{cr_code}")
    return " · ".join(out)


@check("3. every wizard stamps a resolvable source_type")
def _():
    from app.models import JournalEntry
    from app.services.accounting_ops import OPERATIONS
    from app.services.source_reference import _SOURCE_TYPES, _UNKNOWN_LABEL
    seen = []
    for op in OPERATIONS:
        assert op.source_type in _SOURCE_TYPES, (
            f"{op.key}: source_type '{op.source_type}' not registered")
        label = _SOURCE_TYPES[op.source_type][0]
        assert label != _UNKNOWN_LABEL
        assert len(op.source_type) <= 30, "source_type exceeds String(30)"
        e = JournalEntry.query.filter_by(
            company_id=_STATE["cid"], source_type=op.source_type).first()
        assert e is not None, f"{op.key}: no entry carries its source_type"
        seen.append(f"{op.source_type}→{label}")
    return " · ".join(seen)


@check("4. a missing equity account errors cleanly (no 500)")
def _():
    from app.models import Account
    from app.services.accounting_ops import (
        get_operation, run_operation, OperationError,
    )
    acc = _acc("3100")
    saved_code = acc.code
    acc.code = "3100_HIDDEN"          # simulate a pruned chart of accounts
    db.session.commit()
    try:
        run_operation(get_operation("capital"), _STATE["cid"], {
            "amount": "10", "date": date.today().isoformat(),
            # A valid money account, so we reach the EQUITY lookup — the
            # thing under test. Without it the (now required) account
            # field errors first and this check passes for the wrong
            # reason.
            "account_id": str(_acc("1110").id),
        }, actor_id=_STATE["uid"])
        raise AssertionError("expected OperationError")
    except OperationError as e:
        msg = str(e)
        assert "3100" in msg, f"error should name the account: {msg}"
    finally:
        acc.code = saved_code
        db.session.commit()
    return f"clean error: {msg[:48]}"


@check("5. unknown op_key redirects to the index instead of 500")
def _():
    r = _client().get("/accounting-ops/not-a-real-op", follow_redirects=False)
    assert r.status_code == 302, f"status={r.status_code}"
    assert "/accounting-ops/" in r.headers.get("Location", "")
    return "302 → index"


@check("6. the wizard form never asks which account to debit or credit")
def _():
    """MARSOUD-FINANCIAL-ACCOUNT-FIELD — `account_id` used to be banned
    here outright. It is now a legitimate field: which cash or bank
    account the money moved through. What must still never appear is a
    DEBIT/CREDIT account picker — the wizard picks the double entry."""
    from app.services.accounting_ops import OPERATIONS, SELECT_KINDS
    for op in OPERATIONS:
        body = _client().get(f"/accounting-ops/{op.key}").get_data(as_text=True)
        for probe in ('name="debit', 'name="credit',
                      'name="debit_account', 'name="credit_account'):
            assert probe not in body, f"{op.key}: form exposes {probe}"
        assert 'name="amount"' in body, f"{op.key}: no amount field"
        # MARSOUD-OPS-FOUNDATION — an operation may now legitimately ask
        # for more than one account (a transfer has two). What must hold
        # is that EVERY account field is a picker the registry built, so
        # none is a free-text GL number and none can reach an account the
        # picker never offered.
        for f in op.fields:
            if f.kind not in SELECT_KINDS:
                assert "account" not in f.name, (
                    f"{op.key}.{f.name}: an account field that is not a "
                    f"picker (kind={f.kind})")
                continue
            marker = f'name="{f.name}"'
            assert marker in body, f"{op.key}: {f.name} missing from form"
            i = body.index(marker)
            assert body.rfind("<select", 0, i) > body.rfind("<input", 0, i), (
                f"{op.key}: {f.name} renders as a free input, not a picker")
    return "every account field is a registry-built picker"


# ─── Currency ───────────────────────────────────────────────────────────
@check("7. currency_ar maps every code and falls back to the raw code")
def _():
    from flask import current_app
    f = current_app.jinja_env.filters["currency_ar"]
    from app.services.currency import CURRENCY_NAMES_AR
    for code, name in CURRENCY_NAMES_AR.items():
        assert f(code) == name, f"{code} -> {f(code)}"
        assert f(code.lower()) == name, "lookup must be case-insensitive"
    assert f("XYZ") == "XYZ", "unknown code must fall back to the code"
    assert f(None) == "" and f("") == "", "blank must not render 'None'"
    return f"{len(CURRENCY_NAMES_AR)} codes + fallback"


@check("8. no template prints a raw currency code any more")
def _():
    tpl_root = ROOT / "app" / "templates"
    pat = re.compile(
        r"\{\{\s*[\w.]*\b(?:base_currency|currency)\s*\}\}")
    offenders = []
    for p in tpl_root.rglob("*.html"):
        if ".bak" in p.name:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                offenders.append(f"{p.relative_to(tpl_root)}:{i}")
    assert not offenders, "raw currency prints remain: " + ", ".join(offenders[:8])
    return "all currency prints go through |currency_ar"


@check("9. journal form preselects the COMPANY currency, not SAR")
def _():
    body = _client().get("/journals/new").get_data(as_text=True)
    m = re.findall(r'<option value="(\w{3})"\s*\n?\s*selected', body)
    if not m:
        m = [x for x in re.findall(
            r'<option value="(\w{3})"[^>]*selected', body)]
    assert m, "no option is marked selected in the currency picker"
    assert m[0] == "EGP", f"selected currency is {m[0]}, expected EGP"
    assert "جنيه مصري" in body, "picker should show the Arabic name"
    return "EGP preselected from company.base_currency"


@check("10. post_journal with no currency inherits the company's")
def _():
    from app.services.ledger import post_journal
    from app.models import JournalEntry
    cid = _STATE["cid"]
    cash, cap = _acc("1110"), _acc("3100")
    e = post_journal(
        company_id=cid, description="audit — currency inheritance",
        lines=[{"account_id": cash.id, "debit": 5, "credit": 0},
               {"account_id": cap.id, "debit": 0, "credit": 5}],
        source_type="audit_seed",
    )
    row = db.session.get(JournalEntry, e.id)
    assert row.currency == "EGP", (
        f"entry stored currency={row.currency}, expected the company's EGP")
    # An explicit currency must still win (multi-currency invoices need it).
    e2 = post_journal(
        company_id=cid, description="audit — explicit currency wins",
        lines=[{"account_id": cash.id, "debit": 5, "credit": 0},
               {"account_id": cap.id, "debit": 0, "credit": 5}],
        currency="USD", source_type="audit_seed",
    )
    assert db.session.get(JournalEntry, e2.id).currency == "USD"
    return "omitted → EGP; explicit USD still honoured"


def main():
    app = create_app()
    _STATE["app"] = app
    with app.app_context():
        _setup()
    passed = failed = 0
    try:
        for label, fn in CHECKS:
            try:
                with app.app_context():
                    result = fn()
                print(f"PASS  {label}\n        ⇒ {result}")
                passed += 1
            except Exception as e:
                print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
                failed += 1
    finally:
        with app.app_context():
            _teardown()
        print("\n(cleaned up fixture company)")
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
