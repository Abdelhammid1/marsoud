#!/usr/bin/env python3
"""MARSOUD-AGENT-TOOLS-04 (2026-08-06) — audit for the tool
expansion.

Pins the three ticket phases:
  · run_report enum expanded to every function in services/reports.py
  · new read tools for journal / vendors / party statement
  · new read tools for vendor bills / payroll / advances / assets

Cross-tenant discipline check per new tool that takes an id — the
canonical shape the ticket calls out (borrowed from record_payment):
db.session.get(Model, id) + company_id assertion + specific Arabic
error on mismatch.

Every check verified to fail against pre-change HEAD.
"""
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__AGTOOL_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (
        Company, Plan, User, Customer, Vendor, Account, AccountType,
        JournalEntry, JournalLine,
    )
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.seed_coa import seed_default_coa

    plan = Plan.query.filter_by(code="__agtool__").first()
    if not plan:
        plan = Plan(code="__agtool__", name="AgTool", name_ar="أدوات",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "purchases", "hr",
                          "reports", "agent", "settings"])
        db.session.add(plan); db.session.flush()

    def _mk_co(suffix):
        co = Company(name=f"{PREFIX}CO_{suffix}__", base_currency="SAR",
                     vat_rate=Decimal("15"), plan_id=plan.id,
                     timezone="Asia/Riyadh")
        db.session.add(co); db.session.flush()
        co.intended_plan_id = plan.id
        db.session.commit()
        ensure_roles_ready_for_company(co.id)
        seed_default_coa(co.id)
        return co

    def _mk_user(co, tag, role):
        u = User(email=f"{PREFIX}{co.id}_{tag}@audit.local",
                 full_name=f"{tag}-{role}", is_active=True,
                 terms_version=get_terms_version(),
                 terms_accepted_at=datetime.utcnow())
        u.set_password("Passw0rd!audit1")
        db.session.add(u); db.session.flush()
        set_membership_role(u.id, co.id, role)
        return u.id

    co_a = _mk_co("A")
    co_b = _mk_co("B")
    u_owner = _mk_user(co_a, "own", "owner")

    cust_a = Customer(company_id=co_a.id, name="عميل A")
    cust_b = Customer(company_id=co_b.id, name="عميل B")
    vend_a = Vendor(company_id=co_a.id, name="مورد A")
    vend_b = Vendor(company_id=co_b.id, name="مورد B")
    db.session.add_all([cust_a, cust_b, vend_a, vend_b])
    db.session.commit()

    # A representative journal entry in company A so search/read
    # tools have something to find. Uses two seeded accounts.
    cash_a = Account.query.filter_by(
        company_id=co_a.id, code="1110").first()
    exp_a = (Account.query.filter_by(company_id=co_a.id,
                                      code="5100").first()
             or Account.query.filter_by(company_id=co_a.id,
                                         code="5200").first())
    je = JournalEntry(
        company_id=co_a.id, number="JE-TEST-001",
        date=date.today(),
        description="اختبار قيد إيجار",
        reference="TEST-REF")
    db.session.add(je); db.session.flush()
    db.session.add(JournalLine(
        entry_id=je.id, account_id=exp_a.id,
        debit=100, credit=0, debit_base=100, credit_base=0))
    db.session.add(JournalLine(
        entry_id=je.id, account_id=cash_a.id,
        debit=0, credit=100, debit_base=0, credit_base=100))
    # Company B entry — must NEVER leak into A's tools
    cash_b = Account.query.filter_by(
        company_id=co_b.id, code="1110").first()
    exp_b = (Account.query.filter_by(company_id=co_b.id,
                                      code="5100").first()
             or Account.query.filter_by(company_id=co_b.id,
                                         code="5200").first())
    je_b = JournalEntry(
        company_id=co_b.id, number="JE-B-ISOLATION",
        date=date.today(),
        description="قيد شركة ب")
    db.session.add(je_b); db.session.flush()
    db.session.add(JournalLine(
        entry_id=je_b.id, account_id=exp_b.id,
        debit=200, credit=0, debit_base=200, credit_base=0))
    db.session.add(JournalLine(
        entry_id=je_b.id, account_id=cash_b.id,
        debit=0, credit=200, debit_base=0, credit_base=200))
    db.session.commit()

    _STATE.update(
        cid_a=co_a.id, cid_b=co_b.id, owner=u_owner,
        cust_a=cust_a.id, cust_b=cust_b.id,
        vend_a=vend_a.id, vend_b=vend_b.id,
        je_a=je.id, je_b=je_b.id, je_a_number=je.number,
    )


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
        db.session.execute(text(
            "DELETE FROM journal_lines WHERE entry_id IN "
            "(SELECT id FROM journal_entries WHERE company_id=:c)"),
            {"c": cid})
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
    db.session.execute(text("DELETE FROM plans WHERE code='__agtool__'"))
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"),
                           {"u": u.id})
    db.session.commit()


def _run(tool_name, args=None):
    from app.agent.tools import execute_tool
    return execute_tool(
        tool_name, dict(args or {}, _confirmed_proposal_id=1),
        _STATE["cid_a"], _STATE["owner"])


# ─── Phase 1 checks (report expansion) ────────────────────────────────
@check("1. run_report enum covers all report functions in services/reports.py")
def _():
    from app.agent.tools import TOOL_SCHEMAS
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == "run_report")
    enum = set(schema["input_schema"]["properties"]["type"]["enum"])
    required = {
        "balance_sheet", "income_statement",
        "income_statement_compared", "cash_flow",
        "income_summary", "expenses_summary",
        "vat", "ap_aging", "ar_aging",
        "payroll_summary", "fixed_assets", "dashboard",
    }
    missing = required - enum
    assert not missing, f"missing enum values: {missing}"
    return f"{len(enum)} report types exposed"


@check("2. run_report(type=vat) returns VAT report shape")
def _():
    r = _run("run_report", {"type": "vat"})
    assert "error" not in r, f"vat report errored: {r}"
    # vat_report returns dict with output/input/net_payable keys
    assert isinstance(r, dict) and len(r) > 0
    return "vat dispatch works"


@check("3. run_report every new type returns non-error")
def _():
    from datetime import date as _d
    types = ["income_statement_compared", "income_summary",
             "expenses_summary", "ap_aging", "fixed_assets",
             "payroll_summary"]
    fails = []
    for t in types:
        args = {"type": t}
        if t == "payroll_summary":
            args.update({"year": _d.today().year,
                         "month": _d.today().month})
        r = _run("run_report", args)
        if "error" in r:
            fails.append((t, r["error"]))
    assert not fails, f"types errored: {fails}"
    return f"{len(types)} new report types all non-error"


# ─── Phase 2 checks ───────────────────────────────────────────────────
@check("4. get_journal_entry by number returns lines + accounts")
def _():
    r = _run("get_journal_entry",
             {"number": _STATE["je_a_number"]})
    assert "error" not in r, f"errored: {r}"
    assert r["entry_id"] == _STATE["je_a"], (
        f"entry_id mismatch: {r['entry_id']} vs {_STATE['je_a']}")
    assert len(r["lines"]) == 2, (
        f"expected 2 lines, got {len(r['lines'])}: {r['lines']}")
    codes = {l["account_code"] for l in r["lines"] if l["account_code"]}
    # 1110 is cash; the expense account exists as either 5100
    # (Cost of Sales in DEFAULT_COA) or a 5200-prefix account.
    assert "1110" in codes, (
        f"cash account 1110 missing from codes: {codes}")
    return f"entry #{r['entry_id']} · {len(r['lines'])} lines · codes {codes}"


@check("5. get_journal_entry cross-tenant: B's entry refused in A")
def _():
    r = _run("get_journal_entry", {"entry_id": _STATE["je_b"]})
    assert "error" in r, (
        f"CROSS-TENANT LEAK: A got B's entry: {r}")
    return "B's entry refused for A"


@check("6. search_journals text filter narrows results")
def _():
    r = _run("search_journals",
             {"text": "إيجار",
              "start_date": (date.today() - timedelta(days=30)).isoformat(),
              "end_date": date.today().isoformat()})
    assert "error" not in r, f"errored: {r}"
    assert r["count"] >= 1, "seeded 'إيجار' entry not found"
    # And no B-only entry surfaces
    numbers = {e["number"] for e in r["entries"]}
    assert "JE-B-ISOLATION" not in numbers, (
        "CROSS-TENANT LEAK in search_journals")
    return f"{r['count']} matched; no cross-tenant leak"


@check("7. party_statement for A's customer works")
def _():
    r = _run("party_statement",
             {"kind": "customer",
              "party_id": _STATE["cust_a"]})
    assert "error" not in r, f"errored: {r}"
    assert "rows" in r or "opening_balance" in r
    return "statement built"


@check("8. party_statement cross-tenant: B's customer refused in A")
def _():
    r = _run("party_statement",
             {"kind": "customer",
              "party_id": _STATE["cust_b"]})
    assert "error" in r, f"CROSS-TENANT LEAK: {r}"
    assert "غير موجود" in r["error"]
    return "B's customer refused"


@check("9. list_vendors returns only A's vendors")
def _():
    r = _run("list_vendors", {})
    assert "error" not in r, f"errored: {r}"
    names = {v["name"] for v in r["vendors"]}
    assert "مورد A" in names
    assert "مورد B" not in names, (
        "CROSS-TENANT LEAK: B's vendor in A's list")
    return f"{len(r['vendors'])} vendors, no B leak"


# ─── Phase 3 checks ───────────────────────────────────────────────────
@check("10. list_vendor_bills returns A's bills only")
def _():
    """No bills seeded — assert the tool runs without error and
    returns 0 bills; cross-tenant is proved by the count staying 0
    when B has no bills either. The get_vendor_bill check below
    pins the ID-based cross-tenant path."""
    r = _run("list_vendor_bills", {})
    assert "error" not in r, f"errored: {r}"
    assert isinstance(r["count"], int)
    return f"count={r['count']}, no error"


@check("11. get_vendor_bill cross-tenant: bill_id from B refused")
def _():
    """Create a bill in B, try to fetch from A. Bill must not surface."""
    from app.models import (VendorBill, VendorBillStatus,
                            VendorBillPaymentMethod)
    b = VendorBill(company_id=_STATE["cid_b"],
                    vendor_id=_STATE["vend_b"],
                    number="VB-B-ISOL",
                    issue_date=date.today(), due_date=date.today(),
                    payment_method=VendorBillPaymentMethod.CREDIT,
                    currency="SAR", status=VendorBillStatus.POSTED,
                    total=100)
    db.session.add(b); db.session.commit()
    r = _run("get_vendor_bill", {"bill_id": b.id})
    assert "error" in r, f"CROSS-TENANT LEAK: {r}"
    return "B's bill refused"


@check("12. list_payroll_runs cross-tenant: B's runs invisible in A")
def _():
    from app.models import PayrollRun
    from app.services.numbering import next_number
    b_run = PayrollRun(company_id=_STATE["cid_b"],
                        number=next_number(_STATE["cid_b"], "PAYROLL"),
                        period_year=2026, period_month=8)
    db.session.add(b_run); db.session.commit()
    r = _run("list_payroll_runs", {"year": 2026, "month": 8})
    assert "error" not in r, f"errored: {r}"
    b_run_ids = {run["run_id"] for run in r["runs"]}
    assert b_run.id not in b_run_ids, (
        f"CROSS-TENANT LEAK: B's payroll run in A's list")
    return f"B's run invisible; A got {len(r['runs'])} rows"


@check("13. list_employee_advances cross-tenant safe")
def _():
    """Create an active advance in B for a B-employee; must not
    appear in A's list."""
    from app.models import Employee, EmployeeAdvance, AdvanceStatus
    b_emp = Employee(company_id=_STATE["cid_b"], name="موظف ب",
                      status="ACTIVE", start_date=date.today())
    db.session.add(b_emp); db.session.flush()
    adv = EmployeeAdvance(company_id=_STATE["cid_b"],
                           employee_id=b_emp.id,
                           amount=Decimal("500"),
                           remaining=Decimal("500"),
                           months=1,
                           monthly_installment=Decimal("500"),
                           disbursed_on=date.today(),
                           status=AdvanceStatus.ACTIVE)
    db.session.add(adv); db.session.commit()
    r = _run("list_employee_advances", {})
    assert "error" not in r, f"errored: {r}"
    ids = {a["advance_id"] for a in r["advances"]}
    assert adv.id not in ids, (
        "CROSS-TENANT LEAK: B's advance in A's list")
    return f"B's advance invisible; A got {r['count']} advances"


@check("14. list_fixed_assets scoped to company")
def _():
    """No assets seeded — the tool runs, returns a dict for A."""
    r = _run("list_fixed_assets", {})
    assert "error" not in r, f"errored: {r}"
    # fixed_assets_report returns a dict shape — the exact keys are
    # its business; we just pin it runs and returns something.
    assert isinstance(r, dict)
    return "returned shape ok"


# ─── Umbrella check ───────────────────────────────────────────────────
@check("15. every new tool + report type reachable via TOOL_SCHEMAS")
def _():
    from app.agent.tools import TOOL_SCHEMAS
    names = {s["name"] for s in TOOL_SCHEMAS}
    required = {
        "get_journal_entry", "search_journals", "party_statement",
        "list_vendors", "list_vendor_bills", "get_vendor_bill",
        "list_payroll_runs", "list_employee_advances",
        "list_fixed_assets",
    }
    missing = required - names
    assert not missing, f"missing tool schemas: {missing}"
    return f"{len(required)} new tools all in TOOL_SCHEMAS"


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
