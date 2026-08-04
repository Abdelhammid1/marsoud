#!/usr/bin/env python3
"""MARSOUD-SOURCE-LABEL-UNIFY (2026-08-04).

The system had TWO Arabic maps for journal source types:

  _SOURCE_TYPES     app/services/source_reference.py  — party statement
                                                        + stock movements
  SOURCE_LABELS_AR  app/routes/accounts.py            — account ledger

New types were registered in the first and forgotten in the second, three
times running. The account ledger's fallback was `(source_type, None)` —
the RAW English key as the user-facing label — so opening the cash or an
employee's ledger printed `capital_injection` in an RTL Arabic table.

This suite locks in the unification. The load-bearing checks are 2 (the
reported bug, asserted on the real rendered page), 5 (registering a type
in ONE place reaches every screen) and 9 (nothing in the DB renders as
English anywhere).

Checks:
  1. The second map is gone and no new one has appeared.
  2. The account ledger renders Arabic for the three types from the
     ticket — capital_injection / owner_drawings / employee_advance.
  3. The rendered account ledger contains no raw source_type key at all.
  4. Account ledger and party statement agree on the label for a type.
  5. A type registered in ONE place resolves on ALL three screens.
  6. Preserved link: a manual entry still links to its journal entry.
  7. Preserved link: payroll still links to the payroll screen.
  8. Types emitted by services but registered in NEITHER map are covered.
  9. DB coverage: every source_type in the DB has an Arabic label.
 10. The fallback string "قيد يدوي" is defined exactly once.
"""
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
COMPANY_NAME = "__SRC_LABEL_UNIFY__"
EMAIL = "__srclabel@audit.local"
_STATE = {}

# The three from the ticket, plus the four found unregistered in both maps.
TICKET_TYPES = ("capital_injection", "owner_drawings", "employee_advance")
NEWLY_COVERED = ("vendor_payment", "work_order",
                 "work_order_consumption", "work_order_receipt")


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _setup():
    from app.models import Company, User, Plan
    from app.services.seed_coa import seed_default_coa
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.roles import set_membership_role
    from app.services.legal import get_terms_version
    from app.services.plan_gating import plan_allows

    _teardown()

    co = Company(name=COMPANY_NAME, base_currency="EGP", vat_rate=0)
    db.session.add(co)
    db.session.flush()
    # Pick the first plan that actually allows journals, else the ledger
    # pages 403 and every check below fails for the wrong reason.
    for pl in Plan.query.order_by(Plan.id).all():
        co.plan_id = pl.id
        co.intended_plan_id = pl.id
        db.session.flush()
        if plan_allows("journals.create", co):
            break
    db.session.commit()

    seed_default_coa(co.id)
    ensure_roles_ready_for_company(co.id)

    u = User(email=EMAIL, full_name="SrcLabel Owner", is_active=True,
             terms_version=get_terms_version(),
             terms_accepted_at=datetime.utcnow())
    u.set_password("Passw0rd!audit1")
    db.session.add(u)
    db.session.flush()
    db.session.commit()
    set_membership_role(u.id, co.id, "owner")

    _STATE["cid"] = co.id
    _STATE["uid"] = u.id
    _seed_entries(co.id, u.id)


def _seed_entries(cid, uid):
    """One journal per source_type we care about, all touching 1110 so a
    single account ledger shows them together."""
    from app.services.ledger import post_journal, get_account_by_code
    cash = get_account_by_code(cid, "1110")
    cap = get_account_by_code(cid, "3100")
    # The description must NOT contain the source_type key: check 2 scans
    # the whole rendered page for the raw key, and a key echoed in the
    # memo column would make it fail for the wrong reason.
    for st in TICKET_TYPES + ("payroll",):
        post_journal(
            company_id=cid, description="قيد اختبار للمراجعة",
            lines=[{"account_id": cash.id, "debit": 10, "credit": 0},
                   {"account_id": cap.id, "debit": 0, "credit": 10}],
            entry_date=date.today(), created_by=uid,
            source_type=st, source_id=1,
        )
    # A manual entry: no source_type at all.
    post_journal(
        company_id=cid, description="audit — manual",
        lines=[{"account_id": cash.id, "debit": 10, "credit": 0},
               {"account_id": cap.id, "debit": 0, "credit": 10}],
        entry_date=date.today(), created_by=uid,
    )
    _STATE["cash_id"] = cash.id


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text
    co = Company.query.filter_by(name=COMPANY_NAME).first()
    if co:
        cid = co.id
        db.session.execute(text(
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id=:c)"),
            {"c": cid})
        for tbl in ("journal_entries", "payment_methods", "accounts",
                    "user_companies"):
            try:
                db.session.execute(
                    text(f"DELETE FROM {tbl} WHERE company_id=:c"), {"c": cid})
            except Exception:
                db.session.rollback()
        try:
            db.session.execute(text(
                "DELETE FROM role_permissions WHERE role_id IN "
                "(SELECT id FROM roles WHERE company_id=:c)"), {"c": cid})
            db.session.execute(text("DELETE FROM roles WHERE company_id=:c"),
                               {"c": cid})
        except Exception:
            db.session.rollback()
        db.session.execute(text("DELETE FROM companies WHERE id=:c"),
                           {"c": cid})
        db.session.commit()
    u = User.query.filter_by(email=EMAIL).first()
    if u:
        db.session.delete(u)
        db.session.commit()


def _client():
    c = _STATE["app"].test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(_STATE["uid"])
        s["_fresh"] = True
        s["active_company_id"] = _STATE["cid"]
    return c


def _ledger_html():
    r = _client().get(f"/accounts/{_STATE['cash_id']}/ledger")
    assert r.status_code == 200, f"ledger returned {r.status_code}"
    return r.get_data(as_text=True)


class _FakeEntry:
    """The shape _resolve_source reads off a JournalEntry."""

    def __init__(self, source_type, source_id=1, entry_id=1):
        self.source_type = source_type
        self.source_id = source_id
        self.id = entry_id


# ─── 1. the second map is gone ──────────────────────────────────────────
@check("1. accounts.py no longer keeps its own label map")
def _():
    import app.routes.accounts as accounts_mod
    assert not hasattr(accounts_mod, "SOURCE_LABELS_AR"), \
        "SOURCE_LABELS_AR still exists — the maps are still duplicated"
    src = (ROOT / "app/routes/accounts.py").read_text(encoding="utf-8")
    # The name may still be MENTIONED in the docstring explaining why it
    # was removed; what must be gone is the dict itself.
    assert not re.search(r"^\s*SOURCE_LABELS_AR\s*=", src, re.M), \
        "SOURCE_LABELS_AR is still assigned in accounts.py"

    # And no OTHER module has grown a replacement. A source-type map is
    # recognisable as a dict literal whose keys are source_type strings;
    # look for any module besides source_reference that maps two or more
    # known source_type keys to Arabic.
    known = ("invoice_cogs", "vendor_bill_payment", "capital_injection",
             "owner_drawings", "employee_advance", "refund_cogs")
    offenders = []
    for py in list((ROOT / "app").rglob("*.py")):
        if py.name == "source_reference.py":
            continue
        text_ = py.read_text(encoding="utf-8")
        hits = [k for k in known if f'"{k}":' in text_ or f"'{k}':" in text_]
        if len(hits) >= 2:
            offenders.append(f"{py.relative_to(ROOT)} ({', '.join(hits)})")
    assert not offenders, \
        "a second source-type map appeared in: " + "; ".join(offenders)
    return "one map, in source_reference.py"


# ─── 2. THE REPORTED BUG ────────────────────────────────────────────────
@check("2. account ledger shows Arabic for the 3 types from the ticket")
def _():
    from app.services.source_reference import _SOURCE_TYPES
    html = _ledger_html()
    for st in TICKET_TYPES:
        label = _SOURCE_TYPES[st][0]
        assert st not in html, \
            f"account ledger still prints the RAW key '{st}' to the user"
        assert label in html, \
            f"account ledger missing the Arabic label '{label}' for {st}"
    return " · ".join(f"{s}→{_SOURCE_TYPES[s][0]}" for s in TICKET_TYPES)


@check("2b. an EMPLOYEE's ledger carrying a سلفة is all-Arabic too")
def _():
    """The ticket names two screens by hand: «كشف حساب 1110 وحساب موظف
    عليه سلفة». Check 2 covers 1110; this covers the second, on the
    employee's own subsidiary account under 2130 rather than on cash,
    because that is the account an advance actually lands on."""
    from app.models import Employee
    from app.services.subsidiary import party_payroll_account
    from app.services.ledger import post_journal, get_account_by_code
    from app.services.source_reference import _SOURCE_TYPES

    cid = _STATE["cid"]
    emp = Employee(company_id=cid, name="موظف اختبار", basic_salary=5000)
    db.session.add(emp)
    db.session.flush()
    emp_acc = party_payroll_account(emp)
    db.session.commit()

    post_journal(
        company_id=cid, description="قيد اختبار للمراجعة",
        lines=[{"account_id": emp_acc.id, "debit": 500, "credit": 0},
               {"account_id": get_account_by_code(cid, "1110").id,
                "debit": 0, "credit": 500}],
        entry_date=date.today(), created_by=_STATE["uid"],
        source_type="employee_advance", source_id=1,
    )

    r = _client().get(f"/accounts/{emp_acc.id}/ledger")
    assert r.status_code == 200, f"employee ledger returned {r.status_code}"
    html = r.get_data(as_text=True)
    assert "employee_advance" not in html, \
        "the employee ledger still prints the RAW key 'employee_advance'"
    label = _SOURCE_TYPES["employee_advance"][0]
    assert label in html, f"missing the Arabic label '{label}'"
    return f"{emp_acc.code} ({emp.name}) → {label}"


@check("3. the rendered account ledger contains no raw source_type key")
def _():
    from app.services.source_reference import _SOURCE_TYPES
    html = _ledger_html()
    # Isolate the source column cells, then assert none of them is an
    # ASCII identifier (every legitimate label is Arabic).
    leaked = [st for st in _SOURCE_TYPES if re.search(
        r">\s*" + re.escape(st) + r"\s*<", html)]
    assert not leaked, f"raw source_type keys rendered: {leaked}"
    return f"none of {len(_SOURCE_TYPES)} keys leaked"


# ─── 4-5. one source of truth ───────────────────────────────────────────
@check("4. account ledger and party statement agree on every label")
def _():
    from flask import current_app
    from app.routes.accounts import _resolve_source
    from app.services.source_reference import (
        _SOURCE_TYPES, resolve_reference,
    )
    with current_app.test_request_context():
        for st in _SOURCE_TYPES:
            acct_label, _ = _resolve_source(_FakeEntry(st))
            party_label = resolve_reference(st, 1)["label"]
            assert acct_label == party_label, (
                f"{st}: account ledger says '{acct_label}' but the party "
                f"statement says '{party_label}' — the maps have drifted")
    return f"{len(_SOURCE_TYPES)} labels identical on both screens"


@check("5. a type registered in ONE place resolves on ALL screens")
def _():
    """The acceptance criterion: adding a source type must be a one-line
    change. Register a fake type in _SOURCE_TYPES only, then confirm the
    account ledger, the party statement and stock movements all see it
    without any further edit."""
    from flask import current_app
    from app.routes.accounts import _resolve_source
    from app.services import source_reference as sr

    fake = "__audit_new_type__"
    sr._SOURCE_TYPES[fake] = ("عملية اختبارية", None, None)
    try:
        with current_app.test_request_context():
            acct_label, _ = _resolve_source(_FakeEntry(fake))
            party = sr.resolve_reference(fake, 1)["label"]
            # stock movements go through the batched map
            batched = sr.build_reference_map(
                [{"source_type": fake, "source_id": 1}], _STATE["cid"])
            stock = batched[(fake, 1)]["label"]
        assert acct_label == "عملية اختبارية", \
            f"account ledger did not pick up the new type: {acct_label}"
        assert party == "عملية اختبارية"
        assert stock == "عملية اختبارية"
    finally:
        sr._SOURCE_TYPES.pop(fake, None)
    return "one registration reached all 3 screens"


# ─── 6-7. links the old map had, which must survive ─────────────────────
@check("6. preserved link: a manual entry still links to its journal")
def _():
    from flask import current_app
    from app.routes.accounts import _resolve_source
    from app.services.source_reference import UNKNOWN_LABEL
    with current_app.test_request_context():
        label, link = _resolve_source(_FakeEntry(None, None, entry_id=42))
    assert label == UNKNOWN_LABEL, f"manual label changed: {label}"
    assert link and "/journals/" in link and "42" in link, (
        f"manual entry lost its link to the journal entry: {link}")
    return f"manual → {link}"


@check("7. preserved link: payroll still links to the payroll screen")
def _():
    from flask import current_app
    from app.routes.accounts import _resolve_source
    from app.services.source_reference import resolve_reference
    with current_app.test_request_context():
        label, link = _resolve_source(_FakeEntry("payroll", 3))
        party = resolve_reference("payroll", 3)
    assert link and "payroll" in link, f"payroll lost its link: {link}"
    # The unification moved this link INTO the map, so the party
    # statement and stock movements gained it too.
    assert party["url"] and "payroll" in party["url"], (
        "payroll link should now work on the party statement as well")
    return f"payroll → {link} (party statement too)"


# ─── 8-9. coverage ──────────────────────────────────────────────────────
@check("7b. NOT ONE working link from the old map was lost")
def _():
    """The ticket's explicit condition: «أي فرق في الروابط بين الخريطتين
    يتراعى في التوحيد — التوحيد ميضيّعش أي رابط شغال دلوقتي».

    So: the deleted SOURCE_LABELS_AR, verbatim as it stood at
    693b531^, and every entry that produced a link must still produce
    one. Keys that had no link are listed too, so a future edit that
    drops one of them from the map is caught as well."""
    from flask import current_app
    from app.routes.accounts import _resolve_source
    from app.services.source_reference import _SOURCE_TYPES

    OLD_MAP = {
        "invoice": "invoices.view",
        "invoice_cogs": "invoices.view",
        "refund": "invoices.view",
        "refund_cogs": "invoices.view",
        "credit_note": "invoices.view",
        "vendor_bill": "vendor_bills.view",
        "vendor_bill_payment": "vendor_bills.view",
        "payment": "invoices.view",
        "asset": None,
        "depreciation": None,
        "payroll": "payroll.view",       # resolved to payroll.index
        "opening_balance": None,
        "stock_receipt": None,
        "stock_adjustment": None,
        "manual": "journals.view",       # source_type IS NULL
    }
    lost, unlabelled = [], []
    with current_app.test_request_context():
        for st, endpoint in OLD_MAP.items():
            entry = _FakeEntry(None if st == "manual" else st,
                               source_id=7, entry_id=42)
            label, link = _resolve_source(entry)
            if st != "manual" and st not in _SOURCE_TYPES:
                unlabelled.append(st)
            if endpoint and not link:
                lost.append(f"{st} (was → {endpoint})")
    assert not unlabelled, \
        f"keys dropped from the map entirely: {unlabelled}"
    assert not lost, f"links lost in the unification: {lost}"
    linked = sum(1 for e in OLD_MAP.values() if e)
    return f"all {linked} linked keys still link; {len(OLD_MAP)} keys kept"


@check("8. types emitted by services but in NEITHER map are now covered")
def _():
    from flask import current_app
    from app.services.source_reference import _SOURCE_TYPES, UNKNOWN_LABEL
    with current_app.test_request_context():
        for st in NEWLY_COVERED:
            assert st in _SOURCE_TYPES, f"{st} still unregistered"
            label = _SOURCE_TYPES[st][0]
            assert label != UNKNOWN_LABEL, f"{st} falls back to manual"
            assert not re.search(r"[A-Za-z]", label), \
                f"{st} label is not Arabic: {label}"
    return " · ".join(f"{s}→{_SOURCE_TYPES[s][0]}" for s in NEWLY_COVERED)


@check("8b. NO label in the map contains Latin text, DB or no DB")
def _():
    """Check 9 only sees source_types that happen to exist in the DB it
    is run against, so a label with an English acronym hides on any
    environment where that type has never been posted. `pos_void`
    ("إلغاء عملية POS") and `pos_sale` ("بيع POS") slipped through
    exactly that way and only surfaced on a database that had POS
    activity. Sweep the whole map instead."""
    from app.services.source_reference import _SOURCE_TYPES
    latin = {st: label for st, (label, _e, _k) in _SOURCE_TYPES.items()
             if re.search(r"[A-Za-z]", label)}
    assert not latin, (
        "these labels show Latin text to an Arabic-speaking user: "
        + ", ".join(f"{k}={v!r}" for k, v in sorted(latin.items())))
    return f"all {len(_SOURCE_TYPES)} labels are Arabic-only"


@check("9. DB coverage: every source_type in the DB renders in Arabic")
def _():
    """Acceptance criterion 3 — the full sweep. Every distinct
    source_type actually present in journal_entries or stock_movements
    must resolve to an Arabic label through the account-ledger path,
    which is where the raw English used to leak."""
    from flask import current_app
    from sqlalchemy import text
    from app.routes.accounts import _resolve_source
    with db.engine.connect() as conn:
        rows = list(conn.execute(text(
            "SELECT source_type FROM journal_entries "
            "WHERE source_type IS NOT NULL "
            "UNION "
            "SELECT source_type FROM stock_movements "
            "WHERE source_type IS NOT NULL")))
    found = sorted({r[0] for r in rows if r[0]})
    if not found:
        return "skipped: no source_type rows in DB"

    from app.services.source_reference import (
        resolve_reference, build_reference_map, _SOURCE_TYPES,
        UNKNOWN_LABEL,
    )

    def _fault(label, st):
        """Why this label is not acceptable, or None.

        The two failure modes are different problems with different
        fixes, and an earlier version of this check reported both as
        "renders as English". A reviewer read that as "pos_void is not
        registered" when in fact it WAS registered and merely carried
        the Latin acronym "POS" in its label. Name the actual fault.
        """
        if st not in _SOURCE_TYPES:
            return "NOT REGISTERED in _SOURCE_TYPES"
        if label == st:
            return "renders the raw source_type key"
        if label == UNKNOWN_LABEL:
            return f"falls back to {UNKNOWN_LABEL!r}"
        latin = "".join(sorted(set(re.findall(r"[A-Za-z]+", label))))
        if latin:
            return (f"label {label!r} contains Latin text ({latin}) — "
                    "registered, but not Arabic")
        return None

    faults = []
    with current_app.test_request_context():
        for st in found:
            # "في أي شاشة" — all three renderers, not just one.
            acct, _ = _resolve_source(_FakeEntry(st))
            party = resolve_reference(st, 1)["label"]
            stock = build_reference_map(
                [{"source_type": st, "source_id": 1}],
                _STATE["cid"])[(st, 1)]["label"]
            for screen, label in (("كشف حساب", acct),
                                   ("كشف طرف", party),
                                   ("حركات مخزون", stock)):
                fault = _fault(label, st)
                if fault:
                    faults.append(f"[{screen}] {st}: {fault}")
    assert not faults, (
        f"{len(faults)} source_type/screen combinations are not clean "
        "Arabic:\n        " + "\n        ".join(faults))
    return (f"all {len(found)} distinct source_types render in Arabic "
            f"on all 3 screens")


@check("10. no source-reference renderer hardcodes the fallback string")
def _():
    """Scoped deliberately to the files that RENDER a source reference —
    those are the ones that drifted. Elsewhere "قيد يدوي" is a different
    thing entirely (the name of the manual-journal page), and a repo-wide
    ban would be a false positive."""
    renderers = [
        "app/routes/accounts.py",
        "app/services/party_ledger.py",
        "app/templates/inventory/movements.html",
        "app/templates/party_ledger/index.html",
        "app/templates/accounts/ledger.html",
    ]
    hits = []
    for rel in renderers:
        f = ROOT / rel
        if not f.exists():
            continue
        for i, line in enumerate(
                f.read_text(encoding="utf-8").splitlines(), 1):
            if "قيد يدوي" not in line:
                continue
            stripped = line.strip()
            if stripped.startswith(("#", "{#", "*", '"""')):
                continue
            hits.append(f"{rel}:{i}  {stripped[:60]}")
    assert not hits, (
        "these renderers keep their own copy of the fallback label "
        "instead of importing UNKNOWN_LABEL: " + " | ".join(hits))

    # And the canonical definition still exists.
    from app.services.source_reference import UNKNOWN_LABEL
    assert UNKNOWN_LABEL == "قيد يدوي"
    return f"{len(renderers)} renderers all defer to UNKNOWN_LABEL"



def _neutralise_session_cookie_domain(app):
    """A domain-scoped session cookie is never sent to the test client.

    Copied from tests/audit_portal_403.py (MARSOUD-SESSION-COOKIE-DEV-FIX).
    A production-style .env sets SESSION_COOKIE_DOMAIN=.marsoud.com, which
    scopes the cookie to that domain while the test client runs on
    localhost — so the cookie is never sent back, every request answers
    as anonymous, and @login_required bounces it to /login. The run then
    reports 302s and 500s that read as real failures when in fact no
    fixture session ever existed.

    It is irrelevant to what these audits exercise, so neutralise it for
    the run rather than depend on which .env is on the machine.
    """
    domain = app.config.get("SESSION_COOKIE_DOMAIN")
    if domain:
        app.config["SESSION_COOKIE_DOMAIN"] = None
        print(f"NOTE  SESSION_COOKIE_DOMAIN={domain!r} overridden to None "
              f"for this run -- a domain-scoped cookie is never sent "
              f"to the localhost test client.")

def main():
    app = create_app()
    _neutralise_session_cookie_domain(app)
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
            except Exception as e:  # noqa: BLE001
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
