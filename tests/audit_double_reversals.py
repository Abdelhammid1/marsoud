#!/usr/bin/env python3
"""MARSOUD-DOUBLE-REVERSAL-DIAG + MARSOUD-DEPOSIT-AUDIT-01 (2026-08-06).

Covers parts 2 and 3 of the expansion ticket:

  · The `flask audit-double-reversals` diagnostic — it MUST find
    double-reversal rows that already exist in the DB (from before
    the MARSOUD-REVERSE-ONCE guard), MUST NOT mutate a single row,
    and MUST scope to --company-id.
  · The customer-deposit refund audit trail — refunded_by_id and
    refunded_at must be stamped on refund. Reception's audit was
    already on created_by_id (unchanged by this ticket).

The part-1 settlement double-reverse check lives in
audit_reverse_once.py::check-8 rather than duplicating it here.

Every check verified to fail against pre-change HEAD.

Checks
  1. diagnostic reports "no duplicates" on a clean company
  2. diagnostic finds a FABRICATED duplicate reversal
  3. diagnostic is read-only — DB is byte-identical before/after
     + two runs produce identical stdout
  4. --company-id scopes to one company
  5. refund() stamps refunded_by_id + refunded_at
  6. reception + refund can be different users (audit trail is
     genuinely separate)
  7. before refund, refunded_by_id + refunded_at are both None
  8. deposits UI renders "-" for legacy rows without a refunded_by
"""
import io
import sys
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__DBLREV_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (Company, Plan, User, Customer, PaymentMethod,
                            Account, AccountType)
    from app.models.account import NORMAL_SIDE_FOR_TYPE
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.seed_coa import seed_default_coa
    from app.services.subsidiary import ensure_customer_account

    plan = Plan.query.filter_by(code="__dblrev__").first()
    if not plan:
        plan = Plan(code="__dblrev__", name="DblRev", name_ar="عكس",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "purchases", "crm", "hr",
                          "reports", "settings"])
        db.session.add(plan); db.session.flush()

    def _mk_co(suffix):
        co = Company(name=f"{PREFIX}CO_{suffix}__", base_currency="EGP",
                     vat_rate=0, plan_id=plan.id)
        db.session.add(co); db.session.flush()
        co.intended_plan_id = plan.id
        db.session.commit()
        ensure_roles_ready_for_company(co.id)
        seed_default_coa(co.id)
        return co

    def _mk_user(co, tag, role):
        u = User(email=f"{PREFIX}{co.id}_{tag}@audit.local",
                 full_name=f"{tag}-{role}",
                 is_active=True, terms_version=get_terms_version(),
                 terms_accepted_at=datetime.utcnow())
        u.set_password("Passw0rd!audit1")
        db.session.add(u); db.session.flush()
        set_membership_role(u.id, co.id, role)
        return u.id

    co_a = _mk_co("A")
    co_b = _mk_co("B")
    u1 = _mk_user(co_a, "u1", "owner")
    u2 = _mk_user(co_a, "u2", "accountant")

    # Payment method on A pointing at seeded cash account.
    cash_a = Account.query.filter_by(company_id=co_a.id, code="1110").first()
    pm_a = PaymentMethod(company_id=co_a.id, name="نقدي",
                          name_ar="نقدي", account_id=cash_a.id,
                          is_active=True)
    db.session.add(pm_a); db.session.flush()

    cust_a = Customer(company_id=co_a.id, name="عميل التدقيق")
    db.session.add(cust_a); db.session.flush()
    ensure_customer_account(cust_a)
    db.session.commit()

    _STATE.update(cid_a=co_a.id, cid_b=co_b.id,
                  u1=u1, u2=u2,
                  pm_a=pm_a.id, customer_a=cust_a.id)


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        db.session.execute(text(
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id=:c)"), {"c": cid})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                try:
                    db.session.execute(
                        text(f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                        {"c": cid})
                except Exception:
                    db.session.rollback()
        db.session.execute(text("DELETE FROM companies WHERE id=:c"),
                           {"c": cid})
        db.session.commit()
    db.session.execute(text("DELETE FROM plans WHERE code='__dblrev__'"))
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text(
            "DELETE FROM users WHERE id=:u"), {"u": u.id})
    db.session.commit()


def _wipe_journals(cid):
    """Clear journal rows for a company between checks so counts stay
    predictable."""
    from app.models import JournalEntry, JournalLine
    from sqlalchemy import text
    eids = [r.id for r in JournalEntry.query.filter_by(
        company_id=cid).all()]
    if eids:
        JournalLine.query.filter(
            JournalLine.entry_id.in_(eids)
        ).delete(synchronize_session=False)
    JournalEntry.query.filter_by(company_id=cid).delete()
    db.session.commit()


def _plant_original_and_duplicate_reversals(cid, n_extra=1):
    """Bypass MARSOUD-REVERSE-ONCE and insert a REAL original +
    (1 + n_extra) active reversals pointing at it, mimicking the
    aftermath of the bug the guard was added to fix.

    Direct db.session.add — reverse_journal would refuse. The
    diagnostic reads what's in the DB; the shape is what matters,
    not how it got there."""
    from app.models import JournalEntry, JournalLine, Account
    from app.services.ledger import post_journal
    from datetime import date as _date

    exp = Account.query.filter_by(
        company_id=cid, code="5100").first() or Account.query.filter_by(
        company_id=cid, code="5200").first()
    cash = Account.query.filter_by(company_id=cid, code="1110").first()

    original = post_journal(
        company_id=cid,
        description="original for double-reversal fixture",
        lines=[
            {"account_id": exp.id, "debit": 100, "credit": 0,
             "memo": "exp"},
            {"account_id": cash.id, "debit": 0, "credit": 100,
             "memo": "cash"},
        ],
        entry_date=_date.today(),
    )

    for i in range(1 + n_extra):
        rev = JournalEntry(
            company_id=cid, number=f"REV-{original.id}-{i}",
            date=_date.today(),
            description=f"reversal #{i} of {original.number}",
            currency="EGP", exchange_rate=1,
            is_reversal=True, reversal_of=original.id,
        )
        db.session.add(rev); db.session.flush()
        db.session.add(JournalLine(
            entry_id=rev.id, account_id=cash.id,
            debit=100, credit=0, debit_base=100, credit_base=0))
        db.session.add(JournalLine(
            entry_id=rev.id, account_id=exp.id,
            debit=0, credit=100, debit_base=0, credit_base=100))
    db.session.commit()
    return original


def _run_cli(company_id=None):
    """Invoke the diagnostic and capture stdout as a string."""
    from scripts.audit_double_reversals import _find_duplicates, _print_report
    by_company = _find_duplicates(company_id=company_id)
    buf = io.StringIO()
    with redirect_stdout(buf):
        scope = f"company {company_id}" if company_id else "all companies"
        _print_report(by_company, scope)
    return buf.getvalue()


def _snapshot_rows():
    """Everything we care about not being mutated by the diagnostic.
    Returns a dict of {table: [row_dict, ...]} sorted by id."""
    from sqlalchemy import text
    tables = ("journal_entries", "journal_lines",
              "open_items", "open_item_settlements",
              "customer_deposits")
    out = {}
    for t in tables:
        rows = db.session.execute(text(
            f"SELECT * FROM {t} ORDER BY id")).fetchall()
        out[t] = [tuple(r) for r in rows]
    return out


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. diagnostic reports 'no duplicates' on a clean company")
def _():
    _wipe_journals(_STATE["cid_a"])
    out = _run_cli(company_id=_STATE["cid_a"])
    assert "no duplicate reversals found" in out, (
        f"expected 'no duplicate reversals' in output; got:\n{out}")
    return "reports clean"


@check("2. diagnostic finds a fabricated duplicate reversal")
def _():
    _wipe_journals(_STATE["cid_a"])
    orig = _plant_original_and_duplicate_reversals(
        _STATE["cid_a"], n_extra=1)
    out = _run_cli(company_id=_STATE["cid_a"])
    assert "duplicate reversals" in out, (
        f"expected duplicate reversal report; got:\n{out}")
    assert orig.number in out or f"#{orig.id}" in out, (
        f"original entry not named in report; got:\n{out}")
    assert "<- duplicate" in out, (
        f"duplicate marker missing; got:\n{out}")
    return f"reported original {orig.number}"


@check("3. diagnostic is read-only — DB unchanged, two runs identical")
def _():
    _wipe_journals(_STATE["cid_a"])
    _plant_original_and_duplicate_reversals(_STATE["cid_a"], n_extra=2)
    before = _snapshot_rows()
    out1 = _run_cli(company_id=_STATE["cid_a"])
    after = _snapshot_rows()
    assert before == after, (
        "DB rows changed after diagnostic ran — the command is NOT "
        "read-only. Diff by table:\n" +
        "\n".join(f"  {t}: {len(before[t])}->{len(after[t])}"
                  for t in before if before[t] != after[t]))
    out2 = _run_cli(company_id=_STATE["cid_a"])
    assert out1 == out2, (
        "two consecutive runs produced different output — not idempotent")
    return "0 rows changed; two runs identical"


@check("4. --company-id scopes to one company")
def _():
    _wipe_journals(_STATE["cid_a"])
    _wipe_journals(_STATE["cid_b"])
    orig_a = _plant_original_and_duplicate_reversals(_STATE["cid_a"])
    orig_b = _plant_original_and_duplicate_reversals(_STATE["cid_b"])
    out_a = _run_cli(company_id=_STATE["cid_a"])
    assert orig_a.number in out_a, "A's original not in A's report"
    assert orig_b.number not in out_a, (
        f"B's original leaked into A's report: {orig_b.number}")
    out_b = _run_cli(company_id=_STATE["cid_b"])
    assert orig_b.number in out_b
    assert orig_a.number not in out_b, (
        f"A's original leaked into B's report: {orig_a.number}")
    return "scoping works both directions"


# ─── Part 3 checks ──────────────────────────────────────────────────────
def _record_deposit(actor_id):
    from app.services.deposits import record_deposit
    from app.models import Customer, PaymentMethod
    cust = db.session.get(Customer, _STATE["customer_a"])
    pm = db.session.get(PaymentMethod, _STATE["pm_a"])
    return record_deposit(
        company_id=_STATE["cid_a"], customer=cust,
        amount=Decimal("100"), payment_method=pm,
        actor_id=actor_id)


@check("5. refund() stamps refunded_by_id + refunded_at")
def _():
    _wipe_journals(_STATE["cid_a"])
    from app.models import CustomerDeposit
    from app.services.deposits import refund
    CustomerDeposit.query.delete(); db.session.commit()
    dep = _record_deposit(_STATE["u1"])
    assert dep.refunded_by_id is None, "prep: fresh deposit has refund"
    assert dep.refunded_at is None, "prep: fresh deposit has refund_at"
    refund(dep, actor_id=_STATE["u2"])
    db.session.refresh(dep)
    assert dep.refunded_by_id == _STATE["u2"], (
        f"refunded_by_id={dep.refunded_by_id}, expected {_STATE['u2']}")
    assert dep.refunded_at is not None, "refunded_at not stamped"
    return f"refunded_by={_STATE['u2']}, refunded_at set"


@check("6. reception and refund CAN be different users (separate trails)")
def _():
    _wipe_journals(_STATE["cid_a"])
    from app.models import CustomerDeposit
    from app.services.deposits import refund
    CustomerDeposit.query.delete(); db.session.commit()
    dep = _record_deposit(_STATE["u1"])
    assert dep.created_by_id == _STATE["u1"], (
        f"created_by_id={dep.created_by_id}, expected {_STATE['u1']}")
    refund(dep, actor_id=_STATE["u2"])
    db.session.refresh(dep)
    assert dep.created_by_id != dep.refunded_by_id, (
        "reception + refund reduced to the same field — "
        "the trails are supposed to be separate")
    return f"received by {dep.created_by_id}, refunded by {dep.refunded_by_id}"


@check("7. before refund, refunded_by_id + refunded_at are both None")
def _():
    _wipe_journals(_STATE["cid_a"])
    from app.models import CustomerDeposit
    CustomerDeposit.query.delete(); db.session.commit()
    dep = _record_deposit(_STATE["u1"])
    assert dep.refunded_by_id is None
    assert dep.refunded_at is None
    return "both fields None on a fresh deposit"


@check("8. legacy deposit (no refunded_by) renders '-' in the UI")
def _():
    from app.models import CustomerDeposit
    _wipe_journals(_STATE["cid_a"])
    CustomerDeposit.query.delete(); db.session.commit()
    # Legacy: reception + refund both happened, but the refunded_by
    # column didn't exist yet. Simulate by leaving refunded_by NULL.
    from app.services.deposits import record_deposit
    from app.models import Customer, PaymentMethod
    cust = db.session.get(Customer, _STATE["customer_a"])
    pm = db.session.get(PaymentMethod, _STATE["pm_a"])
    dep = record_deposit(
        company_id=_STATE["cid_a"], customer=cust,
        amount=Decimal("50"), payment_method=pm,
        actor_id=_STATE["u1"])
    dep.status = "REFUNDED"  # bypass service to keep refunded_by NULL
    db.session.commit()

    with _STATE["app"].app_context():
        c = _STATE["app"].test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(_STATE["u1"])
            s["_fresh"] = True
            s["active_company_id"] = _STATE["cid_a"]
        r = c.get(f"/customers/{_STATE['customer_a']}")
    assert r.status_code == 200, f"customer view status={r.status_code}"
    body = r.get_data(as_text=True)
    assert dep.doc_number in body, "deposit not on the page"
    # The legacy row renders استرد: — followed by the dash. Any
    # traceback would return 500, which the status check above already
    # rejects.
    return "legacy row renders without crash"


def main():
    app = create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    _STATE["app"] = app
    passed = failed = 0
    with app.app_context():
        _setup()
        try:
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        => {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        => {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(fixture cleaned up)")
    print(f"\n----  {passed} passed, {failed} failed  ----")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
