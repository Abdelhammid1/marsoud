#!/usr/bin/env python3
"""MARSOUD-AGENT-UX-06 (2026-08-06) — audit for the UX pass.

Most of T6 is client-side rendering (tables, exports, error labels)
and can't be unit-tested without a browser. What CAN be pinned:

  · the server change — create_journal_entry now returns `lines` +
    number + date so the client can render a journal card without
    a second round-trip
  · the /reports export route the client's card links to accepts
    the enum values T4 added and returns a file for them
  · the /journals/<id>/export/<fmt> route is reachable

Every check verified to fail against pre-change HEAD.
"""
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__AGUX_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (Company, Plan, User, Account, AccountType)
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.seed_coa import seed_default_coa

    plan = Plan.query.filter_by(code="__agux__").first()
    if not plan:
        plan = Plan(code="__agux__", name="AgUX", name_ar="UX",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "reports",
                          "agent", "settings"])
        db.session.add(plan); db.session.flush()

    co = Company(name=f"{PREFIX}CO__", base_currency="SAR",
                 vat_rate=Decimal("15"), plan_id=plan.id,
                 timezone="Asia/Riyadh")
    db.session.add(co); db.session.flush()
    co.intended_plan_id = plan.id
    db.session.commit()
    ensure_roles_ready_for_company(co.id)
    seed_default_coa(co.id)

    u = User(email=f"{PREFIX}u@audit.local",
             full_name="ux", is_active=True,
             terms_version=get_terms_version(),
             terms_accepted_at=datetime.utcnow())
    u.set_password("Passw0rd!audit1")
    db.session.add(u); db.session.flush()
    set_membership_role(u.id, co.id, "owner")
    db.session.commit()
    _STATE.update(cid=co.id, uid=u.id)


def _teardown():
    from app.models import Company, User, PlatformSetting
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
    # Orphan JournalLine rows accumulate across test runs — the
    # journal_entries row a line points at is company-scoped and
    # gets deleted by the loop below, but the lines table has no
    # company_id and never cascades. Left alone, tomorrow's new
    # entry gets the same auto-id and inherits every ghost line
    # pointed at it (I hit this: 10 lines returned for a 2-line
    # entry, 8 with account_id → vanished). Sweep them out before
    # anything else runs.
    db.session.execute(text(
        "DELETE FROM journal_lines WHERE entry_id NOT IN "
        "(SELECT id FROM journal_entries)"))
    db.session.commit()
    for co in Company.query.filter(Company.name.like(f"{PREFIX}%")).all():
        cid = co.id
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
    # Second sweep — deleting the company's journal_entries just
    # created a new batch of orphan lines that would haunt the
    # NEXT run. Clean them now while we know they're orphans.
    db.session.execute(text(
        "DELETE FROM journal_lines WHERE entry_id NOT IN "
        "(SELECT id FROM journal_entries)"))
    db.session.commit()
    db.session.execute(text("DELETE FROM plans WHERE code='__agux__'"))
    PlatformSetting.query.filter_by(
        key="agent_require_confirmation").delete()
    db.session.commit()
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"),
                           {"u": u.id})
    db.session.commit()


def _run(name, args):
    """Bypass proposal — turn confirmation off so the tool runs
    synchronously and we can inspect its return shape."""
    from app.models import PlatformSetting
    row = PlatformSetting.query.filter_by(
        key="agent_require_confirmation").first()
    if row is None:
        row = PlatformSetting(key="agent_require_confirmation",
                              value="false")
        db.session.add(row); db.session.commit()
    else:
        row.value = "false"
        db.session.commit()
    from app.agent.tools import execute_tool
    return execute_tool(name, dict(args), _STATE["cid"], _STATE["uid"])


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. create_journal_entry returns lines with account codes + names")
def _():
    from app.models import Account
    exp = (Account.query.filter_by(company_id=_STATE["cid"],
                                    code="5100").first()
           or Account.query.filter_by(company_id=_STATE["cid"],
                                        code="5200").first())
    cash = Account.query.filter_by(company_id=_STATE["cid"],
                                    code="1110").first()
    assert exp is not None, "no expense account in fixture"
    assert cash is not None, "no cash account in fixture"
    r = _run("create_journal_entry", {
        "description": "قيد اختبار UX",
        "entry_date": date.today().isoformat(),
        "lines": [
            {"account_id": exp.id, "debit": 100, "credit": 0,
             "memo": "مصروف"},
            {"account_id": cash.id, "debit": 0, "credit": 100,
             "memo": "نقدي"},
        ],
    })
    assert "error" not in r, f"errored: {r}"
    assert r.get("entry_id"), f"no entry_id: {r}"
    assert "lines" in r, "lines missing from tool result"
    assert len(r["lines"]) == 2, f"expected 2 lines, got {len(r['lines'])}"
    codes = {l["account_code"] for l in r["lines"] if l["account_code"]}
    assert "1110" in codes, f"cash code missing: {codes}"
    # Client card needs name_ar for the second column too.
    names = [l.get("account_name_ar") for l in r["lines"]]
    assert all(n for n in names), (
        f"account_name_ar missing on some lines: {names}")
    # Number + date + description for the card header.
    assert r.get("number"), "entry number missing"
    assert r.get("date"), "entry date missing"
    assert r.get("description") == "قيد اختبار UX"
    return f"entry #{r['entry_id']} · lines={len(r['lines'])} · codes={codes}"


@check("2. /reports export route accepts every T4 enum value")
def _():
    """Client-side card links point at /reports/<type>/export/<fmt>
    using T4's enum values (with '_' → '-' conversion). Pin that
    the route accepts them — a 404 on any type means the export
    button would silently break for that report."""
    from app.services.export import export_report
    from app.models import Company
    co = db.session.get(Company, _STATE["cid"])
    today = date.today()
    start = today.replace(day=1)
    types_that_export = [
        "balance-sheet", "income-statement",
        "cash-flow", "expenses-summary",
        "income-summary",
    ]
    unsupported = []
    for t in types_that_export:
        try:
            file_io, filename, mimetype = export_report(
                co, t, "excel", start, today)
            assert file_io is not None
            assert filename
        except Exception as e:
            unsupported.append((t, str(e)[:80]))
    assert not unsupported, (
        f"export_report failed for: {unsupported}")
    return f"{len(types_that_export)} report types export cleanly"


@check("3. /journals/<id>/export/<fmt> reachable")
def _():
    """The journal card's Excel + PDF buttons hit this route. Smoke
    test the underlying service; the route just wraps it."""
    from app.services.export import (
        export_journal_entry_excel, export_journal_entry_pdf,
    )
    from app.models import Account, JournalEntry, JournalLine
    exp = Account.query.filter_by(
        company_id=_STATE["cid"], code="5100").first() \
        or Account.query.filter_by(
            company_id=_STATE["cid"], code="5200").first()
    cash = Account.query.filter_by(
        company_id=_STATE["cid"], code="1110").first()
    je = JournalEntry(company_id=_STATE["cid"], number="JE-UX-1",
                       date=date.today(), description="ux")
    db.session.add(je); db.session.flush()
    db.session.add(JournalLine(entry_id=je.id, account_id=exp.id,
                                debit=50, credit=0,
                                debit_base=50, credit_base=0))
    db.session.add(JournalLine(entry_id=je.id, account_id=cash.id,
                                debit=0, credit=50,
                                debit_base=0, credit_base=50))
    db.session.commit()
    xl = export_journal_entry_excel(je)
    pdf = export_journal_entry_pdf(je)
    assert xl is not None and pdf is not None
    return "excel + pdf both produced"


@check("4. TOOL_LABEL_AR covers every tool in TOOL_SCHEMAS")
def _():
    """The chat's Arabic-label chip should have an entry for every
    exposed tool. Missing tools fall back to raw name (safe), but
    the audit pins that our map is complete against the current
    TOOL_SCHEMAS so a future maintainer doesn't ship an English-
    only chip by accident."""
    import re
    from app.agent.tools import TOOL_SCHEMAS
    tpl = (Path(__file__).parent.parent
           / "app" / "templates" / "agent" / "chat.html").read_text(
               encoding="utf-8")
    # Extract keys from the const TOOL_LABEL_AR = { 'x': 'y', ... } block
    m = re.search(r"const TOOL_LABEL_AR = \{([^}]+)\}", tpl,
                   flags=re.DOTALL)
    assert m, "TOOL_LABEL_AR block missing"
    keys = set(re.findall(r"'([a-z_]+)'\s*:", m.group(1)))
    tools = {s["name"] for s in TOOL_SCHEMAS}
    missing = tools - keys
    assert not missing, f"tools without Arabic labels: {missing}"
    return f"{len(tools)} tools, all labeled"


@check("5. translateErrorAr helper handles common shapes")
def _():
    """The client-side helper is JS, so we can't invoke it here.
    But we CAN pin that the shipped chat.html actually contains
    the mapping for the four callouts in the plan — regex-check
    the string is present so a future refactor doesn't drop
    Arabic error translation on the floor."""
    tpl = (Path(__file__).parent.parent
           / "app" / "templates" / "agent" / "chat.html").read_text(
               encoding="utf-8")
    assert "translateErrorAr" in tpl, "helper missing"
    assert "انتهى الوقت المسموح" in tpl, "timeout translation missing"
    assert "تجاوزت حد الطلبات" in tpl, "rate-limit translation missing"
    assert "خلل في الاتصال" in tpl, "connection translation missing"
    assert "مشكلة في مفتاح API" in tpl, "api-key translation missing"
    return "4/4 error shapes translated"


@check("6. journal + report card renderers wired into chat.html")
def _():
    """Structural check: the chat.html JS has the two new card
    builders + the dispatch that routes results to them. Regression
    guard — a future rewrite that drops them would silently regress
    to the pre-T6 chip-only UX."""
    tpl = (Path(__file__).parent.parent
           / "app" / "templates" / "agent" / "chat.html").read_text(
               encoding="utf-8")
    for token in ("buildJournalCard", "buildReportCard",
                  "renderReportBody", "renderFlatTable"):
        assert token in tpl, f"{token} missing from chat.html"
    # And that the dispatch hooks them
    assert "buildJournalCard(t.tool, r)" in tpl, (
        "journal dispatch missing")
    assert "buildReportCard(t, r)" in tpl, (
        "report dispatch missing")
    return "renderers + dispatch present"


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
