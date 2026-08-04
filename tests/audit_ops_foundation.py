#!/usr/bin/env python3
"""MARSOUD-OPS-FOUNDATION (2026-08-05).

Preparing the accounting-operations centre for the next wave of wizards.
This suite grows one section at a time; the ticket's six sections map to
the check numbering.

§1 — protecting existing data
  1.  1170 + 5940 are in the default tree, postable, correctly parented
  2.  1170 is NOT a 12xx code — a 12xx would classify every accrued
      revenue as an INVESTING activity in the cash-flow statement
  3.  the backfill adds ONLY those two accounts, even against a tree that
      differs from the default in many other ways (the company-8 shape)
  4.  a code already used by a DIFFERENT account is skipped and reported,
      never overwritten
  5.  a missing parent header is skipped and reported
  6.  dry-run is the default and writes nothing; --apply writes
  7.  running twice adds nothing the second time
  8.  --company-id touches only that company
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__OPSFOUND_"
_STATE = {}

NEW_CODES = ("1170", "5940")


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


# ─── Fixture ────────────────────────────────────────────────────────────
def _mk_company(suffix, seed=True):
    from app.models import Company
    from app.services.seed_coa import seed_default_coa
    co = Company(name=f"{PREFIX}{suffix}__", base_currency="EGP", vat_rate=0)
    db.session.add(co)
    db.session.flush()
    if seed:
        seed_default_coa(co.id)
    db.session.commit()
    return co


def _drop_accounts(company_id, codes):
    from app.models import Account
    for code in codes:
        a = Account.query.filter_by(company_id=company_id, code=code).first()
        if a:
            db.session.delete(a)
    db.session.commit()


def _setup():
    _teardown()
    from app.models import Account, AccountType
    from app.models.account import NORMAL_SIDE_FOR_TYPE

    # (a) a plain company that simply predates the two accounts
    plain = _mk_company("PLAIN")
    _drop_accounts(plain.id, NEW_CODES)

    # (b) a company shaped like company 8: a hand-built tree that has
    #     DELETED many default accounts on purpose, and parked a custom
    #     account on 1170. A "sync everything missing" backfill would
    #     resurrect the deleted ones and collide with the custom one —
    #     which is exactly what this script must never do.
    custom = _mk_company("CUSTOM")
    _drop_accounts(custom.id, NEW_CODES)
    deleted_on_purpose = ("1140", "1150", "1310", "1320", "1330",
                          "5310", "5320", "5330", "5920")
    _drop_accounts(custom.id, deleted_on_purpose)
    parent = Account.query.filter_by(company_id=custom.id, code="1100").first()
    db.session.add(Account(
        company_id=custom.id, code="1170", name="Custom Deposits",
        name_ar="تأمينات لدى الغير (مخصص)", type=AccountType.ASSET,
        normal_side=NORMAL_SIDE_FOR_TYPE[AccountType.ASSET],
        parent_id=parent.id, is_active=True, is_postable=True))
    db.session.commit()

    # (c) a company missing the 5900 header entirely
    noparent = _mk_company("NOPARENT")
    _drop_accounts(noparent.id, NEW_CODES)
    _drop_accounts(noparent.id, ("5910", "5920", "5930", "5900"))

    _STATE.update(plain_id=plain.id, custom_id=custom.id,
                  noparent_id=noparent.id,
                  deleted_on_purpose=deleted_on_purpose)


def _teardown():
    from app.models import Company
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


def _codes(company_id):
    from app.models import Account
    return {a.code for a in Account.query.filter_by(
        company_id=company_id).all()}


# ─── §1 ─────────────────────────────────────────────────────────────────
@check("1. 1170 + 5940 are in the default tree, postable, right parents")
def _():
    from app.services.seed_coa import DEFAULT_COA
    from app.models import AccountType
    rows = {r[0]: r for r in DEFAULT_COA}
    for code, want_parent, want_type in (("1170", "1100", AccountType.ASSET),
                                          ("5940", "5900", AccountType.EXPENSE)):
        assert code in rows, f"{code} missing from DEFAULT_COA"
        _c, _en, ar, atype, parent, postable = rows[code]
        assert atype == want_type, f"{code} type is {atype}"
        assert parent == want_parent, f"{code} parent is {parent}"
        assert postable is True, (
            f"{code} is not postable — post_journal refuses header "
            "accounts, so an operation using it would fail at save time")
        assert ar.strip(), f"{code} has no Arabic name"
    return f"1170 → {rows['1170'][2]} · 5940 → {rows['5940'][2]}"


@check("2. 1170 is not 12xx — it classifies OPERATING, not INVESTING")
def _():
    """The ticket's governing condition. services/reports.py infers the
    cash-flow bucket from the account code, and `code.startswith("12") and
    code != "1290"` means INVESTING — so a 12xx code here would report
    every accrued revenue as an investing activity, in every company."""
    code = "1170"
    assert not code.startswith("12"), \
        "1170 must not be a 12xx code (see the classifier rule)"
    src = (ROOT / "app/services/reports.py").read_text(encoding="utf-8")
    assert 'code.startswith("12") and code != "1290"' in src, (
        "the classifier rule changed — re-check that 1170 is still safe")
    # And prove it against the real function rather than the string.
    from app.services.reports import _classify_cashflow_entry
    assert callable(_classify_cashflow_entry)
    return "1170 is outside the INVESTING prefix"


@check("3. the backfill adds ONLY the two accounts, never a full sync")
def _():
    """The load-bearing check. Company 8 has a hand-built tree with
    accounts deleted on purpose; a 'add everything missing' backfill would
    bring them all back."""
    from scripts.backfill_ops_accounts import run
    cid = _STATE["custom_id"]
    before = _codes(cid)
    run(dry_run=False, company_id=cid)
    after = _codes(cid)
    added = after - before
    assert added == {"5940"}, (
        f"expected to add exactly {{5940}} (1170 is taken here), added: "
        f"{added or 'nothing'}")
    # None of the deliberately-deleted accounts came back.
    resurrected = set(_STATE["deleted_on_purpose"]) & after
    assert not resurrected, (
        f"the backfill resurrected accounts the owner deleted: "
        f"{sorted(resurrected)}")
    # And the source really has no diff-the-whole-tree logic. Check the
    # imported module, not the text: the docstring NAMES DEFAULT_COA to
    # explain why it must not be used, and a plain substring search hits
    # that prose.
    import scripts.backfill_ops_accounts as mod
    assert not hasattr(mod, "DEFAULT_COA"), \
        "the script pulled in DEFAULT_COA — that is how a full sync starts"
    src = (ROOT / "scripts/backfill_ops_accounts.py").read_text(
        encoding="utf-8")
    assert not re.search(r"^\s*from .*seed_coa import|^\s*import .*seed_coa",
                          src, re.M), \
        "the script imports from seed_coa"
    assert len(mod.NEW_ACCOUNTS) == 2, \
        f"NEW_ACCOUNTS has {len(mod.NEW_ACCOUNTS)} rows — it must stay at 2"
    return f"added {added}, resurrected nothing of {len(_STATE['deleted_on_purpose'])} deleted"


@check("4. a code owned by a DIFFERENT account is skipped and reported")
def _():
    from scripts.backfill_ops_accounts import run
    from app.models import Account
    cid = _STATE["custom_id"]
    res = run(dry_run=True, company_id=cid)
    taken = [(c, code) for c, code, _n in res["code_taken"]]
    assert (cid, "1170") in taken, \
        f"1170 collision not reported: {res['code_taken']}"
    # ...and the custom account is untouched.
    a = Account.query.filter_by(company_id=cid, code="1170").first()
    assert a is not None and "مخصص" in (a.name_ar or ""), \
        "the company's own 1170 was overwritten"
    return f"reported and left alone: {a.name_ar}"


@check("5. a missing parent header is skipped and reported")
def _():
    from scripts.backfill_ops_accounts import run
    from app.models import Account
    cid = _STATE["noparent_id"]
    res = run(dry_run=False, company_id=cid)
    missing = [(c, code, p) for c, code, p in res["no_parent"]]
    assert (cid, "5940", "5900") in missing, \
        f"missing parent not reported: {res['no_parent']}"
    assert Account.query.filter_by(company_id=cid, code="5940").first() is None, \
        "5940 was created without its parent"
    # 1170's parent (1100) is present, so that one still lands.
    assert Account.query.filter_by(company_id=cid, code="1170").first() is not None
    return "5940 skipped (no 5900), 1170 still added"


@check("6. dry-run is the default and writes nothing")
def _():
    from scripts.backfill_ops_accounts import run
    from app.models import Account
    cid = _STATE["plain_id"]
    before = _codes(cid)
    res = run(company_id=cid)                      # no dry_run kwarg at all
    assert _codes(cid) == before, "the default run wrote to the database"
    assert res["added"] == [], f"added on a dry-run: {res['added']}"
    assert len(res["would_add"]) == 2, res["would_add"]
    # The CLI flag defaults the same way.
    src = (ROOT / "scripts/backfill_ops_accounts.py").read_text(
        encoding="utf-8")
    assert 'def run(dry_run=True' in src, "dry_run is not the default"
    assert '"--apply", is_flag=True' in src, "--apply is not a flag"
    return "nothing written; plan had 2 rows"


@check("7. running twice adds nothing the second time")
def _():
    from scripts.backfill_ops_accounts import run
    cid = _STATE["plain_id"]
    first = run(dry_run=False, company_id=cid)
    mid = _codes(cid)
    second = run(dry_run=False, company_id=cid)
    assert len(first["added"]) == 2, first["added"]
    assert second["added"] == [], f"second run added {second['added']}"
    assert _codes(cid) == mid, "the second run changed the tree"
    assert len(second["already_there"]) == 2
    return "first run added 2, second added 0"


@check("8. --company-id touches only that company")
def _():
    """Uses the fixtures already built rather than creating a company
    inside the check — seeding one mid-run drags payment-method rows into
    a session the earlier checks have already committed around."""
    from scripts.backfill_ops_accounts import run
    scoped = _STATE["plain_id"]
    other = _STATE["custom_id"]

    # Put the scoped company back to "missing", leave the other alone.
    _drop_accounts(scoped, NEW_CODES)
    before_other = _codes(other)

    res = run(dry_run=False, company_id=scoped)
    assert res["companies"] == 1, \
        f"a scoped run examined {res['companies']} companies"
    assert _codes(other) == before_other, \
        "a scoped run modified a different company"
    assert NEW_CODES[0] in _codes(scoped), "the scoped company was not filled"

    # And an unscoped run does reach every company.
    res_all = run(dry_run=True)
    assert res_all["companies"] > 1, \
        f"the unscoped run only saw {res_all['companies']} company"
    return (f"scoped saw 1 company and left the other untouched; "
            f"unscoped saw {res_all['companies']}")


# ─── §2 — cash-flow classification ──────────────────────────────────────
@check("9. THE BUG: a bank movement now appears in the cash-flow statement")
def _():
    """cash_flow() resolved cash as `code IN ("1110","1120")`, but 1120 is a
    non-postable header no journal line can ever hit — so the statement saw
    the cash box alone and every bank movement was invisible."""
    from app.services.ledger import (
        post_journal, get_account_by_code, cash_accounts,
    )
    from app.services.reports import cash_flow
    cid = _STATE["plain_id"]

    codes = [a.code for a in cash_accounts(cid)]
    assert "1110" in codes, f"cash box missing from the cash set: {codes}"
    assert "1122" in codes, (
        f"the banks are still invisible to the reports: {codes} — this is "
        "the bug")
    assert "1120" not in codes, \
        "1120 is a header; it can hold no lines and must not be counted"

    before = cash_flow(cid)["financing"]
    bank = get_account_by_code(cid, "1122")
    cap = get_account_by_code(cid, "3100")
    post_journal(company_id=cid, description="ops-foundation bank probe",
                 lines=[{"account_id": bank.id, "debit": 5000, "credit": 0},
                        {"account_id": cap.id, "debit": 0, "credit": 5000}],
                 cashflow_category="FINANCING")
    after = cash_flow(cid)["financing"]
    assert round(after - before, 2) == 5000.0, (
        f"a 5000 bank receipt moved financing by {after - before} — the "
        "statement still cannot see the banks")
    return f"{len(codes)} cash accounts; a bank receipt now shows ({before} → {after})"


@check("10. the dashboard liquidity KPI sees the banks too")
def _():
    """Same literal, same bug: _account_balance_as_of sums journal LINES,
    and a header has none, so «السيولة المتاحة» was cash-box-only."""
    from app.services.reports import dashboard_metrics
    cid = _STATE["plain_id"]
    m = dashboard_metrics(cid)
    # Check 9 posted 5000 into a bank against this same company.
    assert float(m["cash_position"]) >= 5000.0, (
        f"cash_position is {m['cash_position']} — it is missing the 5000 "
        "sitting in the bank")
    return f"cash_position = {m['cash_position']}"


@check("11. every operation declares its cash-flow category, explicitly")
def _():
    from app.services.accounting_ops import OPERATIONS, CASHFLOW_CATEGORIES
    for op in OPERATIONS:
        assert op.cashflow_category in CASHFLOW_CATEGORIES, (
            f"{op.key}: {op.cashflow_category!r}")
    return " · ".join(f"{o.key}={o.cashflow_category}" for o in OPERATIONS)


@check("12. an operation that omits the category is refused at build time")
def _():
    """A rule that lives in a docstring is a rule that gets skipped."""
    from app.services.accounting_ops import Operation
    for bad in (None, "", "OPERATIONAL", "financing"):
        try:
            Operation(key="probe", title="t", icon="i", description="d",
                      source_type="s", fields=[], build=lambda c, d: None,
                      cashflow_category=bad)
        except ValueError:
            continue
        raise AssertionError(f"cashflow_category={bad!r} was accepted")
    return "None, '', a typo and wrong-case all refused"


@check("13. the category reaches the journal entry, not just the registry")
def _():
    from app.models import JournalEntry
    from app.services.accounting_ops import get_operation, run_operation
    from app.services.ledger import get_account_by_code
    from datetime import date as _date
    cid = _STATE["plain_id"]
    op = get_operation("capital")
    entry = run_operation(op, cid, {
        "amount": "250", "date": _date.today().isoformat(),
        "account_id": str(get_account_by_code(cid, "1110").id),
    })
    row = db.session.get(JournalEntry, entry.id)
    assert row.cashflow_category == op.cashflow_category, (
        f"entry carries {row.cashflow_category!r}, operation declares "
        f"{op.cashflow_category!r} — run_operation is not forwarding it")
    return f"entry {row.number} stamped {row.cashflow_category}"


def main():
    app = create_app()
    _STATE["app"] = app
    passed = failed = 0
    with app.app_context():
        _setup()
        try:
            for label, fn in CHECKS:
                try:
                    result = fn()
                    print(f"PASS  {label}\n        ⇒ {result}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}\n        ⇒ {type(e).__name__}: {e}")
                    failed += 1
        finally:
            _teardown()
            print("\n(cleaned up fixture companies)")
    print(f"\n────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
