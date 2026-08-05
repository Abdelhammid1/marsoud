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

§2 — the cash-flow statement
  9.  THE BUG the ticket did not know about: cash was resolved as
      `code IN ("1110","1120")`, but 1120 is a non-postable HEADER no
      journal line can hit — every bank movement was invisible
  10. the dashboard liquidity KPI had the same literal, same bug
  11. every operation declares its cash-flow category
  12. an operation that omits it is refused at build time
  13. the declared category reaches the journal entry

§3 — the new input kinds
  14. every declared kind is one the template can render, through ONE
      shared select branch rather than a branch per kind
  15. an unknown kind is refused at build time
  16. every account picker offers POSTABLE accounts only — enforced once
      in ledger.postable_under, not re-implemented per kind
  17. a crafted POST cannot reach an account the picker never showed
      (header / wrong root / another tenant)
  18. the party picker returns the party's SUB-account, never the header

§4 — the open item
  19. a two-sided operation writes its row and links it both ways;
      run_operation never passed source_id before this ticket, so
      reversal could not find anything
  20. the settle wizard has no free amount box — it settles an ITEM
  21. the picker offers open items only, each labelled with its remainder
  22. an item cannot be settled beyond its remainder
  23. a settled item cannot be settled again, and every leg is kept
      separately (EmployeeAccrual overwrites one column and loses them)
  24. reversing a settlement reopens the item and keeps the leg
  25. reversing the creating journal cancels the item
  26. a transfer moves real money and appears nowhere in the statement
  26b THE ONE THAT BITES: the accrual payable is 2160, so inference says
      FINANCING; the operation declares OPERATING and 500 of real cash
      lands in the right section. This is what §2 actually buys — the
      transfer nets to zero regardless of what it is called.
  27. a transfer onto the same account is refused
  28. a failed two-sided operation leaves no orphan row behind

§5 — hard boundaries
  Both of these produce a journal that BALANCES, which is why they need a
  guard: nothing downstream objects and the damage surfaces later, in a
  report nobody reconciles.
  29. an operation can refuse a party, and does
  30. an operation can refuse tax, and does
  31. the guards do not fire on ordinary submissions — a guard that
      refuses everything is worse than no guard
  32. an unknown boundary is refused at build time
  33. forbidding a party while ASKING for one is a build-time error
  34. InvoiceStatus.WRITTEN_OFF exists and is excluded from AR aging
  35. a WRITTEN_OFF invoice really stops ageing (1000 -> 0)
  36. every status is either excluded or aged ON PURPOSE — the guard that
      stops the next one slipping through an exclusion list silently

  HONEST SCOPE, as planned: §5's acceptance criterion is about the GENERIC
  expense/revenue operations, which this ticket does not build. The guards
  land now so those inherit them; today they are demonstrated on the
  accrual pair. Likewise the write-off OPERATION is out of scope — the
  status and its exclusion are in, because the tuples fail silently.

§6 — permissions and grouping
  Every wizard sat behind ONE gate, journals.create, so "let the cashier
  move money from the till to the bank" was indistinguishable from "let
  the cashier inject capital and record owner drawings".
  37. every operation declares a permission and a group
  38. a new permission is in ALL THREE places — P, PERMISSION_CATALOG and
      _IMPLIES. Miss the catalog and an owner cannot grant it; miss
      _IMPLIES and every CUSTOM role loses the wizard on deploy, because
      roles_seed never re-syncs a custom role
  39. the three original operations did not change gate — nobody loses
      access on deploy day
  40. opening an operation's URL without its permission is refused, for
      GET and for POST. Hiding the card is presentation; this is the
      protection
  41. and with the permission every operation still renders — a gate that
      denies everyone would pass 40 and be useless
  42. the index renders the groups, hardcodes none of them, and omits the
      families that are empty
  43. an unknown group or a missing permission is a build-time error
"""
import re
import sys
from datetime import date
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

    # ─── §6 needs real requests ─────────────────────────────────────────
    # An owner of the plain company, on a plan that actually allows
    # journals.create — plan_allows has no trial bypass, so on the wrong
    # plan every request would redirect and §6's checks would "pass"
    # without ever reaching the permission code they are testing.
    #
    # NB the Flask-Login trap this repo keeps hitting: inside ONE
    # app_context every test-client request answers as the FIRST user to
    # log in, because g._login_user is cached. There is exactly one user
    # here. Do not add a second and expect it to work.
    from datetime import datetime
    from app.models import Plan, User
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.roles import set_membership_role
    from app.services.legal import get_terms_version
    from app.services.plan_gating import plan_allows

    for pl in Plan.query.order_by(Plan.id).all():
        plain.plan_id = pl.id
        plain.intended_plan_id = pl.id
        db.session.flush()
        if plan_allows("journals.create", plain):
            break
    db.session.commit()
    ensure_roles_ready_for_company(plain.id)

    u = User(email="__opsfound@audit.local", full_name="OpsFound Owner",
             is_active=True, terms_version=get_terms_version(),
             terms_accepted_at=datetime.utcnow())
    u.set_password("Passw0rd!audit1")
    db.session.add(u)
    db.session.flush()
    db.session.commit()
    set_membership_role(u.id, plain.id, "owner")

    _STATE.update(uid=u.id, cid=plain.id)


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
    for u in User.query.filter(
            User.email.like("__opsfound@audit.local")).all():
        db.session.execute(text("DELETE FROM user_companies WHERE user_id=:u"),
                           {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"), {"u": u.id})
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
                      cashflow_category=bad,
                      group="equity", permission="journals.create")
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


# ─── §3 the new field kinds ─────────────────────────────────────────────
def _op(key):
    from app.services.accounting_ops import get_operation
    return get_operation(key)


def _run(key, cid, **form):
    from app.services.accounting_ops import run_operation
    from datetime import date as _date
    form.setdefault("date", _date.today().isoformat())
    return run_operation(_op(key), cid, form, actor_id=_STATE.get("uid"))


def _aged_total(report, customer_name):
    """What the aging report says one customer owes, in total."""
    for row in report["rows"]:
        if row.get("customer") == customer_name or \
                row.get("name") == customer_name:
            return round(float(row.get("total") or 0), 2)
    return 0.0


def _client():
    c = _STATE["app"].test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(_STATE["uid"])
        sess["_fresh"] = True
        sess["active_company_id"] = _STATE["cid"]
    return c


def _balance(account_id):
    """Net debit-minus-credit on one account, straight from the lines."""
    from app.models import JournalEntry, JournalLine
    rows = (db.session.query(JournalLine)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .filter(JournalLine.account_id == account_id,
                    JournalEntry.is_active.is_(True)).all())
    return round(sum(float(r.debit_base) - float(r.credit_base)
                     for r in rows), 2)


def _money(cid, code="1110"):
    from app.services.ledger import get_account_by_code
    return str(get_account_by_code(cid, code).id)


@check("14. every declared field kind is one the template can render")
def _():
    """A typo in a kind used to fall through to a plain text input and ship
    a broken wizard silently. Field now refuses an unknown kind — and the
    template must handle every kind that exists, not merely the ones in
    use today."""
    from app.services.accounting_ops import (
        FIELD_KINDS, SELECT_KINDS, OPERATIONS,
    )
    tpl = (ROOT / "app/templates/accounting_ops/run.html").read_text(
        encoding="utf-8")
    assert SELECT_KINDS <= FIELD_KINDS, "SELECT_KINDS has a kind Field rejects"
    for kind in FIELD_KINDS - SELECT_KINDS:
        assert kind in tpl, f"template cannot render kind {kind!r}"
    # And the selects go through ONE branch, not one per kind.
    assert "select_kinds" in tpl, (
        "the template branches per select kind instead of using the shared "
        "set — every new picker would need a template edit")
    used = {f.kind for op in OPERATIONS for f in op.fields}
    return f"{len(FIELD_KINDS)} kinds, {len(used)} in use, one select branch"


@check("15. an unknown field kind is refused at build time")
def _():
    from app.services.accounting_ops import Field
    try:
        Field("x", "س", "sparkle_account")
        raise AssertionError("Field accepted an unknown kind")
    except ValueError as e:
        assert "sparkle_account" in str(e)
    return "unknown kind rejected"


@check("16. every account picker offers POSTABLE accounts only")
def _():
    """Enforced once in ledger.postable_under, not re-implemented per kind.
    A header refuses journal lines, so offering one is a guaranteed error
    at save time with a message the user cannot act on."""
    from app.models import Account
    from app.services.accounting_ops import (
        OPERATIONS, field_choices, ACCOUNT_KIND_ROOTS,
    )
    cid = _STATE["plain_id"]
    seen = 0
    for op in OPERATIONS:
        ch = field_choices(op, cid)
        for f in op.fields:
            if f.kind not in ACCOUNT_KIND_ROOTS and f.kind not in (
                    "financial_account", "financial_account_to"):
                continue
            ids = [v for _, opts in ch.get(f.name, []) for v, _ in opts]
            assert ids, f"{op.key}.{f.name}: picker is empty"
            for aid in ids:
                a = db.session.get(Account, aid)
                assert a.is_postable, (
                    f"{op.key}.{f.name} offers {a.code} which is a header")
                assert a.company_id == cid, "picker leaked another tenant"
                seen += 1
    return f"{seen} offered accounts, all postable, all in-tenant"


@check("17. a crafted POST cannot reach an account the picker never showed")
def _():
    """resolve_* validates against the OFFERED SET, not merely "is this an
    account of mine" — otherwise a hand-edited form posts an expense
    straight onto a bank, or onto a header."""
    from app.services.accounting_ops import OperationError
    from app.services.ledger import get_account_by_code
    cid = _STATE["plain_id"]
    header = get_account_by_code(cid, "1120")          # non-postable
    revenue = get_account_by_code(cid, "4100")         # wrong root
    foreign = get_account_by_code(_STATE["custom_id"], "1110")   # other tenant
    blocked = []
    for label, bad in (("header", header.id), ("wrong root", revenue.id),
                       ("other tenant", foreign.id)):
        try:
            _run("accrue-expense", cid, amount="10", expense_account_id=bad)
            raise AssertionError(f"accrue-expense accepted a {label} account")
        except OperationError:
            blocked.append(label)
    for label, bad in (("header", header.id), ("other tenant", foreign.id)):
        try:
            _run("capital", cid, amount="10", account_id=bad)
            raise AssertionError(f"capital accepted a {label} money account")
        except OperationError:
            pass
    return "refused: " + ", ".join(blocked)


@check("18. the party picker returns the SUB-account, never the header")
def _():
    from app.models import Customer
    from app.services.accounting_ops import party_choices, resolve_party
    from app.services.ledger import get_account_by_code
    cid = _STATE["plain_id"]
    c = Customer(company_id=cid, name="__OPSFOUND cust__")
    db.session.add(c)
    db.session.commit()
    groups = party_choices(cid)
    values = [v for _, opts in groups for v, _ in opts]
    assert f"customer:{c.id}" in values, "the new customer is not offered"
    party, account, label = resolve_party(cid, f"customer:{c.id}")
    header = get_account_by_code(cid, "1130")
    assert account.id != header.id, "resolve_party returned the 1130 header"
    assert account.is_postable, "the party sub-account is not postable"
    assert account.parent_id == header.id, (
        "the sub-account does not sit under 1130")
    return f"{label} -> {account.code} (postable, under 1130)"


# ─── §4 the open item ───────────────────────────────────────────────────
@check("19. a two-sided operation writes its row and links it both ways")
def _():
    """run_operation never passed source_id before this ticket, so
    _undo_source_side_effects could never fire for an operation."""
    from app.models import JournalEntry, OpenItem
    from app.services.ledger import postable_under
    cid = _STATE["plain_id"]
    exp = postable_under(cid, "5000")[0]
    entry = _run("accrue-expense", cid, amount="1000",
                 expense_account_id=str(exp.id), notes="audit")
    row = db.session.get(JournalEntry, entry.id)
    assert row.source_type == "open_item", f"source_type {row.source_type!r}"
    assert row.source_id, "the journal carries no source_id — reversal is blind"
    item = db.session.get(OpenItem, row.source_id)
    assert item is not None and item.company_id == cid
    assert item.journal_entry_id == row.id, "the item does not link back"
    assert float(item.original_amount) == 1000.0
    assert item.remaining == 1000.0 and item.status.value == "OPEN"
    _STATE["item_id"] = item.id
    return f"item {item.id} <-> entry {row.number}, remaining {item.remaining}"


@check("20. the settle wizard has no free amount box — it settles an ITEM")
def _():
    op = _op("settle-accrued-expense")
    kinds = {f.name: f.kind for f in op.fields}
    assert kinds.get("open_item_id") == "open_item", (
        "the settle wizard does not pick an item — a payment not tied to "
        "what it settles is how the same accrual gets paid twice")
    assert [f for f in op.fields if f.name == "open_item_id"][0].required
    return "settlement is tied to an open item"


@check("21. the picker offers open items only, and shows the remainder")
def _():
    from app.services.accounting_ops import field_choices
    from app.services.open_items import open_items_for
    cid = _STATE["plain_id"]
    ch = field_choices(_op("settle-accrued-expense"), cid)
    offered = [(v, lbl) for _, opts in ch["open_item_id"] for v, lbl in opts]
    ids = [v for v, _ in offered]
    assert _STATE["item_id"] in ids, "the open accrual is not offered"
    for _, lbl in offered:
        assert "متبق" in lbl, (
            f"the label {lbl!r} omits the remainder — two accruals with the "
            "same description would be indistinguishable")
    live = {i.id for i in open_items_for(cid, kind="accrued_expense")}
    assert set(ids) == live, "the picker and the open set disagree"
    return f"{len(offered)} open item(s), each labelled with its remainder"


@check("22. an item cannot be settled beyond its remainder")
def _():
    from app.models import OpenItem
    from app.services.accounting_ops import OperationError
    cid = _STATE["plain_id"]
    iid = _STATE["item_id"]
    _run("settle-accrued-expense", cid, amount="400",
         open_item_id=str(iid), account_id=_money(cid))
    item = db.session.get(OpenItem, iid)
    db.session.refresh(item)
    assert item.remaining == 600.0, f"remaining {item.remaining} after 400"
    assert item.status.value == "PARTIALLY_SETTLED", item.status
    try:
        _run("settle-accrued-expense", cid, amount="600.01",
             open_item_id=str(iid), account_id=_money(cid))
        raise AssertionError("over-settlement was allowed")
    except OperationError as e:
        msg = str(e)
    db.session.refresh(item)
    assert item.remaining == 600.0, "the refused settlement still moved money"
    return f"400 of 1000 settled; 600.01 refused ({msg[:40]})"


@check("23. a fully settled item cannot be settled again")
def _():
    from app.models import OpenItem
    from app.services.accounting_ops import OperationError
    from app.services.open_items import open_items_for
    cid = _STATE["plain_id"]
    iid = _STATE["item_id"]
    _run("settle-accrued-expense", cid, amount="600",
         open_item_id=str(iid), account_id=_money(cid))
    item = db.session.get(OpenItem, iid)
    db.session.refresh(item)
    assert item.status.value == "SETTLED" and item.remaining == 0.0, (
        f"{item.status} remaining {item.remaining}")
    assert iid not in {i.id for i in open_items_for(cid)}, (
        "a closed item is still on offer in the picker")
    try:
        _run("settle-accrued-expense", cid, amount="1",
             open_item_id=str(iid), account_id=_money(cid))
        raise AssertionError("a closed item was settled again")
    except OperationError:
        pass
    assert len([s for s in item.settlements if not s.reversed_at]) == 2, (
        "the settlement legs were not kept individually — EmployeeAccrual "
        "overwrites one column and loses the earlier payments")
    return "closed, off the picker, 2 legs kept separately"


@check("24. reversing a settlement reopens the item and keeps the leg")
def _():
    from app.models import OpenItem
    from app.services.ledger import reverse_journal
    item = db.session.get(OpenItem, _STATE["item_id"])
    leg = [s for s in item.settlements if not s.reversed_at][-1]
    reverse_journal(leg.journal_entry_id)
    db.session.refresh(item)
    db.session.refresh(leg)
    assert item.status.value == "PARTIALLY_SETTLED", (
        f"a settled item whose journal was reversed stayed {item.status}")
    assert item.remaining == 600.0, f"remaining {item.remaining}, expected 600"
    assert leg.reversed_at is not None, "the leg was not stamped reversed"
    assert leg in item.settlements, (
        "the leg was deleted — the money did move once and that is history")
    return f"reopened to {item.status.value}, remaining {item.remaining}"


@check("25. reversing the creating journal cancels the item")
def _():
    """A never-settled item. The partly-paid case is check 44: the guard
    refuses it, because reversing an accrual that has already been paid
    strands the paid amount on the payable."""
    from app.models import OpenItem, JournalEntry
    from app.services.ledger import reverse_journal, postable_under
    from app.services.open_items import open_items_for
    cid = _STATE["plain_id"]
    exp = str(postable_under(cid, "5000")[0].id)
    entry = _run("accrue-expense", cid, amount="750",
                 expense_account_id=exp, notes="never settled")
    item = db.session.get(
        OpenItem, db.session.get(JournalEntry, entry.id).source_id)
    assert not item.settlements, "fixture error: this item should be clean"
    reverse_journal(item.journal_entry_id)
    db.session.refresh(item)
    assert item.status.value == "CANCELLED", (
        f"the item survived its own reversal as {item.status}")
    assert item.reversal_entry_id, "the reversal entry was not recorded"
    assert item.id not in {i.id for i in open_items_for(cid)}, (
        "a cancelled item is still settleable")
    return "cancelled, off the picker, reversal entry recorded"


@check("26. a transfer really moves the money, and shows up nowhere in the "
       "cash-flow statement")
def _():
    """Both legs land on cash accounts, so cash_flow nets the entry to zero
    before the category is ever consulted — the statement cannot see it
    whatever it is called. NONCASH is therefore the honest label rather
    than the mechanism; what this check pins is that the SHAPE holds. If a
    future edit ever gave the transfer a non-cash leg (a bank fee, say),
    the entry would stop netting out and the category would start to
    matter — the two-cash-legs assertion is what would catch that."""
    from app.models import JournalEntry
    from app.services.ledger import cash_account_ids
    from app.services.reports import cash_flow
    cid = _STATE["plain_id"]
    src = int(_money(cid, "1110"))
    dst = int(_money(cid, "1122"))
    before = cash_flow(cid)
    src_before = _balance(src)
    dst_before = _balance(dst)

    entry = _run("transfer", cid, amount="900",
                 account_id=str(src), account_id_to=str(dst))
    row = db.session.get(JournalEntry, entry.id)
    assert row.cashflow_category == "NONCASH", row.cashflow_category

    # The money genuinely moved.
    assert round(_balance(src) - src_before, 2) == -900.0, "source"
    assert round(_balance(dst) - dst_before, 2) == 900.0, "destination"

    # Every leg is a cash account — this is why it nets out.
    ids = set(cash_account_ids(cid))
    assert all(l.account_id in ids for l in row.lines), (
        "a transfer leg landed outside the cash set — the entry no longer "
        "nets to zero and its category now changes the statement")

    after = cash_flow(cid)
    for key in ("operating", "investing", "financing"):
        moved = float(after[key]) - float(before[key])
        assert abs(moved) < 0.005, (
            f"a transfer moved {key} by {moved} — it must appear nowhere "
            "in the statement")
    return "900 moved 1110 -> 1122; both legs cash; statement unchanged"


@check("26b. THE ONE THAT BITES: settling an accrual is OPERATING, which "
       "inference gets wrong")
def _():
    """This is what §2's «every operation declares its category» actually
    buys. The accrued-expense payable is 2160, and the classifier's rule is
    `code.startswith("21") -> FINANCING`. So a cash payment of an accrued
    operating expense would be reported as a FINANCING outflow — real
    money in the wrong section of the statement. Measured on the seeded
    tree: inference FINANCING, declared OPERATING, 500 of cash."""
    from app.models import JournalEntry
    from app.services.ledger import (
        post_journal, get_account_by_code, cash_account_ids, postable_under,
    )
    from app.services.reports import _classify_cashflow_entry, cash_flow
    cid = _STATE["plain_id"]
    ids = cash_account_ids(cid)

    # What inference alone would have said about this exact journal.
    payable = get_account_by_code(cid, "2160")
    probe = post_journal(
        company_id=cid, description="__inference probe__",
        lines=[{"account_id": payable.id, "debit": 500, "credit": 0},
               {"account_id": int(_money(cid)), "debit": 0, "credit": 500}])
    inferred = _classify_cashflow_entry(probe, ids)
    probe.is_active = False          # keep the probe out of the statement
    db.session.commit()
    assert inferred == "FINANCING", (
        f"inference now says {inferred}, not FINANCING — this check's "
        "premise has changed and its numbers need rechecking")

    # What the operation actually produces.
    exp = postable_under(cid, "5000")[0]
    acc = _run("accrue-expense", cid, amount="500",
               expense_account_id=str(exp.id))
    item_id = db.session.get(JournalEntry, acc.id).source_id
    before = cash_flow(cid)
    entry = _run("settle-accrued-expense", cid, amount="500",
                 open_item_id=str(item_id), account_id=_money(cid))
    row = db.session.get(JournalEntry, entry.id)
    assert row.cashflow_category == "OPERATING", row.cashflow_category
    after = cash_flow(cid)

    op_moved = round(float(after["operating"]) - float(before["operating"]), 2)
    fin_moved = round(float(after["financing"]) - float(before["financing"]), 2)
    assert op_moved == -500.0, (
        f"operating moved {op_moved}, expected -500 — the declared category "
        "is not reaching the statement")
    assert fin_moved == 0.0, (
        f"financing moved {fin_moved} — the payment is being reported as a "
        "financing activity, which is the bug the explicit category fixes")
    return (f"inference={inferred}, declared=OPERATING; "
            f"operating {op_moved}, financing {fin_moved}")


@check("27. a transfer to the same account is refused")
def _():
    from app.services.accounting_ops import OperationError
    cid = _STATE["plain_id"]
    try:
        _run("transfer", cid, amount="10", account_id=_money(cid),
             account_id_to=_money(cid))
        raise AssertionError("a transfer onto itself was allowed")
    except OperationError as e:
        return str(e)


@check("28. a failed two-sided operation leaves no orphan row behind")
def _():
    """The builder flushes the item BEFORE post_journal runs. If posting
    fails, that flushed row is still pending in the session, and the next
    unrelated commit would persist an open item whose journal never
    existed."""
    from app.models import OpenItem
    from app.services.accounting_ops import OperationError
    cid = _STATE["plain_id"]
    before = OpenItem.query.filter_by(company_id=cid).count()
    try:
        _run("accrue-expense", cid, amount="50", expense_account_id="999999")
        raise AssertionError("an invalid expense account was accepted")
    except OperationError:
        pass
    db.session.commit()          # anything left pending would land here
    after = OpenItem.query.filter_by(company_id=cid).count()
    assert after == before, f"{after - before} orphan item(s) survived"
    return f"{before} items before and after the failure"


# ─── §5 hard boundaries ─────────────────────────────────────────────────
@check("29. an operation can refuse a party, and does")
def _():
    """An expense with a supplier belongs in the bills module, which drives
    the vendor sub-account under 2110. Posted straight to cash the journal
    still BALANCES — nothing objects, and the vendor's statement quietly
    disagrees with the ledger from then on."""
    from app.services.accounting_ops import OperationError, get_operation
    from app.services.ledger import postable_under
    cid = _STATE["plain_id"]
    op = get_operation("accrue-expense")
    assert "party" in op.forbids, "the expense wizard declares no party boundary"
    exp = str(postable_under(cid, "5000")[0].id)
    refused = []
    for key in ("party", "vendor_id", "supplier_id"):
        try:
            _run("accrue-expense", cid, amount="10",
                 expense_account_id=exp, **{key: "7"})
            raise AssertionError(f"{key} was accepted")
        except OperationError as e:
            assert "المورد" in str(e), f"{key}: unhelpful message {e}"
            refused.append(key)
    return "refused: " + ", ".join(refused)


@check("30. an operation can refuse tax, and does")
def _():
    """Input VAT belongs in purchases, which drives 1280. Post the gross to
    the expense account instead and the entry balances, the expense is
    overstated, and the reclaimable VAT is simply gone."""
    from app.services.accounting_ops import OperationError, get_operation
    from app.services.ledger import postable_under
    cid = _STATE["plain_id"]
    assert "tax" in get_operation("accrue-expense").forbids
    exp = str(postable_under(cid, "5000")[0].id)
    refused = []
    for key in ("tax", "tax_amount", "vat"):
        try:
            _run("accrue-expense", cid, amount="10",
                 expense_account_id=exp, **{key: "1.5"})
            raise AssertionError(f"{key} was accepted")
        except OperationError as e:
            assert "ضريبة" in str(e), f"{key}: unhelpful message {e}"
            refused.append(key)
    return "refused: " + ", ".join(refused)


@check("31. the guards do not fire on ordinary submissions")
def _():
    """A guard that refuses everything is worse than no guard. The normal
    path must stay open, and an EMPTY stray field must not trip it."""
    from app.services.ledger import postable_under
    cid = _STATE["plain_id"]
    exp = str(postable_under(cid, "5000")[0].id)
    entry = _run("accrue-expense", cid, amount="10",
                 expense_account_id=exp, vendor_id="", tax="")
    assert entry is not None
    return f"blank stray fields ignored; {entry.number} posted"


@check("32. an unknown boundary is refused at build time")
def _():
    from app.services.accounting_ops import Operation, Field
    try:
        Operation(key="x", title="x", icon="x", description="x",
                  source_type="capital_injection",
                  fields=[Field("amount", "المبلغ", "amount")],
                  build=lambda *a, **k: None, cashflow_category="OPERATING",
                  group="equity", permission="journals.create",
                  forbids=("sparkles",))
        raise AssertionError("an unknown boundary was accepted")
    except ValueError as e:
        assert "sparkles" in str(e)
    return "unknown boundary rejected"


@check("33. forbidding a party while ASKING for one is a build-time error")
def _():
    """Otherwise the operation refuses every submission it receives, and
    the contradiction only shows up as a user complaint."""
    from app.services.accounting_ops import Operation, Field
    try:
        Operation(key="x", title="x", icon="x", description="x",
                  source_type="capital_injection",
                  fields=[Field("party_id", "الطرف", "party")],
                  build=lambda *a, **k: None, cashflow_category="OPERATING",
                  group="equity", permission="journals.create",
                  forbids=("party",))
        raise AssertionError("the contradiction was accepted")
    except ValueError as e:
        assert "party_id" in str(e)
    return "contradiction caught at import time"


# ─── §5.3 the write-off status ──────────────────────────────────────────
@check("34. InvoiceStatus.WRITTEN_OFF exists and is excluded from AR aging")
def _():
    """The aging tuple is an EXCLUSION list, so a status added without
    touching it ages forever — the report keeps claiming money already
    given up on, and nothing fails. The status and its exclusion land
    together for exactly that reason.

    HONEST SCOPE: the write-off OPERATION is not in this ticket."""
    from app.models import InvoiceStatus
    from app.models.invoice import NON_RECEIVABLE_STATUSES
    assert hasattr(InvoiceStatus, "WRITTEN_OFF")
    assert InvoiceStatus.WRITTEN_OFF in NON_RECEIVABLE_STATUSES, (
        "WRITTEN_OFF is not excluded from receivables — a forgiven debt "
        "would age forever")
    # It must be distinct from the statuses that already exist: a written
    # off invoice is not cancelled (the sale happened) and not refunded
    # (no money went back).
    for other in ("CANCELLED", "REFUNDED", "VOIDED", "PAID"):
        assert InvoiceStatus.WRITTEN_OFF is not getattr(InvoiceStatus, other)
    src = (ROOT / "app/services/reports.py").read_text(encoding="utf-8")
    assert "NON_RECEIVABLE_STATUSES" in src, (
        "the aging report is back on an inline tuple — the whole point is "
        "that the set is named in one place")
    return f"{len(NON_RECEIVABLE_STATUSES)} statuses owe nothing"


@check("35. a WRITTEN_OFF invoice does not age")
def _():
    """Proved against the report, not against the tuple."""
    from datetime import timedelta
    from app.models import Customer, Invoice, InvoiceStatus
    from app.services.reports import aging_report
    cid = _STATE["plain_id"]
    cust = Customer(company_id=cid, name="__OPSFOUND writeoff__")
    db.session.add(cust)
    db.session.flush()
    inv = Invoice(
        company_id=cid, customer_id=cust.id, number="__WO-1__",
        issue_date=date.today() - timedelta(days=120),
        due_date=date.today() - timedelta(days=90),
        subtotal=1000, total=1000, paid_amount=0,
        status=InvoiceStatus.SENT)
    db.session.add(inv)
    db.session.commit()

    aged = _aged_total(aging_report(cid), cust.name)
    assert aged >= 1000.0, (
        f"an unpaid overdue invoice is not ageing at all ({aged}) — this "
        "check cannot prove anything")

    inv.status = InvoiceStatus.WRITTEN_OFF
    db.session.commit()
    after = _aged_total(aging_report(cid), cust.name)
    assert after == 0.0, (
        f"a written-off invoice is still ageing at {after} — the report is "
        "claiming money that was forgiven")
    return f"aged {aged:.0f} while SENT, {after:.0f} once written off"


@check("36. every invoice status is either excluded or aged on purpose")
def _():
    """The guard the ticket actually asks for: the next status added must
    be a decision, not an accident. If this fails, name the new status in
    NON_RECEIVABLE_STATUSES or in AGED_ON_PURPOSE below."""
    from app.models import InvoiceStatus
    from app.models.invoice import NON_RECEIVABLE_STATUSES
    AGED_ON_PURPOSE = {
        "DRAFT",              # not sent, but its balance is real if it is
        "SENT",
        "PARTIALLY_PAID",
        "OVERDUE",
        "PARTIALLY_REFUNDED",  # ages at its remaining balance
    }
    excluded = {s.name for s in NON_RECEIVABLE_STATUSES}
    unaccounted = {s.name for s in InvoiceStatus} - excluded - AGED_ON_PURPOSE
    assert not unaccounted, (
        f"status(es) {sorted(unaccounted)} are neither excluded from AR "
        "aging nor listed as deliberately aged — decide which, because an "
        "exclusion list fails silently")
    return f"{len(excluded)} excluded, {len(AGED_ON_PURPOSE)} aged on purpose"


# ─── §6 permissions and grouping ────────────────────────────────────────
@check("37. every operation declares a permission and a group")
def _():
    from app.services.accounting_ops import OPERATIONS, GROUP_KEYS
    from app.services.permissions import P
    seen = {}
    for op in OPERATIONS:
        assert op.permission, f"{op.key}: no permission"
        assert op.group in GROUP_KEYS, f"{op.key}: group {op.group!r}"
        assert op.permission in P, (
            f"{op.key}: permission {op.permission!r} is not in the legacy "
            "role map — has_permission would deny everyone with a role "
            "that predates the DB permission rows")
        seen.setdefault(op.permission, []).append(op.key)
    return " · ".join(f"{k}: {len(v)}" for k, v in seen.items())


@check("38. a new permission is registered in ALL THREE places")
def _():
    """Miss the catalog and the owner cannot grant it. Miss _IMPLIES and
    every CUSTOM role loses the wizard on deploy, because roles_seed never
    re-syncs a custom role."""
    from app.services.accounting_ops import OPERATIONS
    from app.services.permissions import P, _IMPLIES
    from app.services.roles_seed import PERMISSION_CATALOG
    new = {op.permission for op in OPERATIONS} - {"journals.create"}
    assert new, "no per-operation permission was actually introduced"
    for code in sorted(new):
        assert code in P, f"{code} missing from P (permissions.py)"
        assert code in PERMISSION_CATALOG, (
            f"{code} missing from PERMISSION_CATALOG — an owner cannot "
            "grant a permission that the roles screen never lists")
        assert _IMPLIES.get(code) == "journals.create", (
            f"{code} is not implied by journals.create — every role that "
            "could run these wizards yesterday would lose them today")
    return f"{len(new)} codes in P + catalog + _IMPLIES"


@check("39. the three original operations did not change gate")
def _():
    """Nobody loses access on deploy day."""
    from app.services.accounting_ops import get_operation
    for key in ("capital", "opening-balance", "owner-drawings"):
        op = get_operation(key)
        assert op.permission == "journals.create", (
            f"{key} moved to {op.permission!r} — existing roles would lose "
            "an operation they have today")
    return "capital / opening-balance / owner-drawings still journals.create"


@check("40. opening an operation URL without its permission is refused")
def _():
    """The load-bearing one. Hiding a card is presentation; the URL is
    guessable, so the check has to live in the route."""
    from app.services import permissions as perms
    from app.services.accounting_ops import OPERATIONS
    client = _client()

    real = perms.has_permission
    denied = {"ops.transfer"}

    def fake(action, user=None, company=None):
        if action in denied:
            return False
        return real(action, user=user, company=company)

    perms.has_permission = fake
    # The route imported the name directly, so patch it there too.
    import app.routes.accounting_ops as route_mod
    route_real = route_mod.has_permission
    route_mod.has_permission = fake
    try:
        r = client.get("/accounting-ops/transfer", follow_redirects=False)
        assert r.status_code in (301, 302), (
            f"GET on a forbidden operation returned {r.status_code}, not a "
            "redirect — the wizard rendered for someone without the "
            "permission")
        r = client.post("/accounting-ops/transfer",
                        data={"amount": "10"}, follow_redirects=False)
        assert r.status_code in (301, 302), (
            f"POST on a forbidden operation returned {r.status_code} — the "
            "journal may have been posted")
        # And the card is gone from the index.
        body = client.get("/accounting-ops/").get_data(as_text=True)
        assert "/accounting-ops/transfer" not in body, (
            "the index still links an operation the user cannot run")
        # while the ones they DO have are still there
        assert "/accounting-ops/capital" in body, (
            "filtering the index removed operations the user still has")
    finally:
        perms.has_permission = real
        route_mod.has_permission = route_real
    return "GET redirected, POST redirected, card hidden, others intact"


@check("41. with permission, every operation is reachable")
def _():
    """The other half of check 40 — a gate that denies everyone passes 40
    and is useless."""
    from app.services.accounting_ops import OPERATIONS
    client = _client()
    for op in OPERATIONS:
        r = client.get(f"/accounting-ops/{op.key}")
        assert r.status_code == 200, (
            f"{op.key} returned {r.status_code} for a user who HAS "
            f"{op.permission}")
        assert op.title in r.get_data(as_text=True)
    return f"all {len(OPERATIONS)} wizards render for a permitted user"


@check("42. the index renders the groups, and hardcodes none of them")
def _():
    from app.services.accounting_ops import OPERATIONS, GROUP_LABELS
    body = _client().get("/accounting-ops/").get_data(as_text=True)
    used = {op.group for op in OPERATIONS}
    for key in used:
        assert GROUP_LABELS[key] in body, (
            f"group «{GROUP_LABELS[key]}» is not on the page")
    # An unused group must NOT be rendered as an empty heading.
    for key, label in GROUP_LABELS.items():
        if key not in used:
            assert label not in body, (
                f"empty group «{label}» is rendered with nothing in it")
    tpl = (ROOT / "app/templates/accounting_ops/index.html").read_text(
        encoding="utf-8")
    for label in GROUP_LABELS.values():
        assert label not in tpl, (
            f"«{label}» is hardcoded in the template — the page must stay "
            "registry-driven")
    assert "has_permission" not in tpl, (
        "the template is asking about permissions; the route hands it an "
        "already-filtered list")
    return f"{len(used)} groups rendered, {len(GROUP_LABELS) - len(used)} empty ones omitted"


@check("43. an unknown group or a missing permission is a build-time error")
def _():
    from app.services.accounting_ops import Operation, Field
    base = dict(key="x", title="x", icon="x", description="x",
                source_type="capital_injection",
                fields=[Field("amount", "المبلغ", "amount")],
                build=lambda *a, **k: None, cashflow_category="OPERATING")
    for label, kwargs in (
        ("unknown group", dict(group="sparkles", permission="journals.create")),
        ("blank permission", dict(group="equity", permission="")),
        ("no permission", dict(group="equity", permission=None)),
    ):
        try:
            Operation(**base, **kwargs)
            raise AssertionError(f"{label} was accepted")
        except ValueError:
            pass
    return "unknown group, blank and missing permission all refused"


# ─── Found by auditing the batch, not by building it ────────────────────
@check("44. reversing the creation of a PARTLY PAID item is refused")
def _():
    """Found by adversarial probe, not by the build.

    Accrue 1000, settle 500, then reverse the accrual. The reversal
    credits back the full 1000 while the settlement had already debited
    2160 by 500 — leaving 2160 at +500. That is real money which left the
    bank, now stranded on a payable whose item is marked CANCELLED, so no
    screen will ever offer to clear it. BOTH journals balance, so nothing
    downstream ever complains. Measured before the guard: 2160 = 500.00.

    The rule is the accounting one: you cannot un-accrue what you have
    already partly paid."""
    from app.models import OpenItem
    from app.services.ledger import (
        reverse_journal, LedgerError, postable_under,
    )
    cid = _STATE["plain_id"]
    exp = str(postable_under(cid, "5000")[0].id)
    e = _run("accrue-expense", cid, amount="1000", expense_account_id=exp)
    item = OpenItem.query.filter_by(journal_entry_id=e.id).first()
    _run("settle-accrued-expense", cid, amount="500",
         open_item_id=str(item.id), account_id=_money(cid))
    db.session.refresh(item)
    payable_before = _balance(item.account_id)

    try:
        reverse_journal(e.id)
        raise AssertionError(
            "reversing the creation of a partly-paid item was ALLOWED — "
            "the amount already paid is now stranded on the payable")
    except LedgerError as err:
        msg = str(err)
        assert "السداد" in msg, f"unhelpful message: {msg}"

    db.session.rollback()
    db.session.refresh(item)
    assert item.status.value == "PARTIALLY_SETTLED", (
        f"the refused reversal still changed the item to {item.status}")
    assert round(_balance(item.account_id) - payable_before, 2) == 0.0, (
        "the refused reversal still moved the payable")
    return f"refused: {msg[:56]}"


@check("45. and the RIGHT order still unwinds to zero")
def _():
    """The other half of 44 — a guard that refuses the correct sequence
    too would pass 44 and make the operation unusable. Reverse the
    settlement first, then the creation: both accounts must return to
    where they started."""
    from app.models import OpenItem
    from app.services.ledger import reverse_journal, postable_under
    cid = _STATE["plain_id"]
    exp = str(postable_under(cid, "5000")[0].id)
    cash_id = int(_money(cid))

    from app.services.ledger import get_account_by_code
    payable_start = _balance(get_account_by_code(cid, "2160").id)
    cash_start = _balance(cash_id)

    e = _run("accrue-expense", cid, amount="1000", expense_account_id=exp)
    item = OpenItem.query.filter_by(journal_entry_id=e.id).first()

    e2 = _run("settle-accrued-expense", cid, amount="500",
              open_item_id=str(item.id), account_id=str(cash_id))
    reverse_journal(e2.id)
    db.session.refresh(item)
    assert item.status.value == "OPEN", (
        f"reversing the only settlement left the item {item.status}")

    reverse_journal(e.id)
    db.session.refresh(item)
    assert item.status.value == "CANCELLED", item.status
    assert round(_balance(item.account_id) - payable_start, 2) == 0.0, (
        f"the payable did not return to {payable_start}")
    assert round(_balance(cash_id) - cash_start, 2) == 0.0, (
        f"cash did not return to {cash_start}")
    return "settlement then creation: payable and cash both back to zero"


@check("46. nan and inf are refused before they reach the database")
def _():
    """Found by adversarial probe. float() accepts "nan", and nan fails
    EVERY comparison — `nan <= 0` is False — so it passed validation,
    passed the over-settlement check, and was written to a NOT NULL
    column. The user got a 500, not a message."""
    from app.services.accounting_ops import _amount, OperationError
    from app.services.open_items import _clean_amount, OpenItemError
    refused = []
    for raw in ("nan", "NaN", "inf", "-inf", "Infinity"):
        for fn, exc in ((_amount, OperationError), (_clean_amount, OpenItemError)):
            try:
                fn({"amount": raw} if fn is _amount else raw)
                raise AssertionError(f"{fn.__name__} accepted {raw!r}")
            except exc:
                pass
        refused.append(raw)
    # ...and an absurd magnitude, which Numeric(15,2) would silently
    # truncate.
    try:
        _amount({"amount": "1e300"})
        raise AssertionError("1e300 was accepted")
    except OperationError:
        pass
    # while ordinary input, including Arabic-Indic digits, still works
    assert _amount({"amount": "100"}) == 100.0
    assert _amount({"amount": "١٠٠"}) == 100.0, (
        "Arabic-Indic digits stopped working — this is an Arabic UI")
    return f"refused {', '.join(refused)} + 1e300; ١٠٠ still reads as 100"


@check("47. the model and the migration declare the same FKs")
def _():
    """A DB built by create_all() (fresh dev, some fixtures) and one built
    by the migration must not diverge silently."""
    mig = (ROOT / "migrations/versions/j0s3o6u9n4p5_open_items.py").read_text(
        encoding="utf-8")
    model = (ROOT / "app/models/open_item.py").read_text(encoding="utf-8")
    assert mig.count('ondelete="CASCADE"') == model.count('ondelete="CASCADE"'), (
        f"migration declares {mig.count(chr(39))} CASCADE FKs and the model "
        f"declares a different number — the two schemas disagree")
    from app.models import OpenItem, OpenItemSettlement
    cols = {c.name for c in OpenItem.__table__.columns}
    assert {"company_id", "kind", "account_id", "original_amount",
            "settled_amount", "status", "journal_entry_id"} <= cols
    return f"{mig.count('ondelete=\"CASCADE\"')} CASCADE FKs on both sides"


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
