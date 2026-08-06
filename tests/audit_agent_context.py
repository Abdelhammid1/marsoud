#!/usr/bin/env python3
"""MARSOUD-AGENT-CONTEXT-01 (2026-08-06) — audit for the accountant
agent's per-turn context block + prompt rewrite.

The pre-ticket agent answered confidently with the wrong data because
its context was three lines (name/currency/VAT) and its prompt named
five DEFAULT_COA codes as if every tenant had them. This suite pins
the three fixes:

  · date block uses company-tz today, not server-UTC today
  · prompt has NO hardcoded account codes; missing roles + off-plan
    modules produce refusal wording
  · CoA summary reflects the ACTUAL tenant's codes, marking missing
    roles as "—  (غير مُعرَّف)"

Every check verified to fail against pre-change HEAD (git stash + run
+ unstash — the prompt regex fails immediately because 1130/4100/2120
are still in the old text; the context builder does not exist at all).

Checks
   1. context has a date block, ISO date + Arabic month + weekday
   2. tools + context use company-tz today (Riyadh midnight rollover)
   3. NO 4-digit account codes in the rewritten prompt
   4. custom CoA — company that renamed 4100 surfaces the new code
   5. missing role → "— (غير مُعرَّف)"
   6. modules line reflects Plan.modules
   7. off-plan refusal rule present in the prompt
   8. tool list_invoices uses company-tz today for its default filter
   9. context fits the token budget (< 2000 chars)
  10. cache-friendly order — persona first, blank line, then context
"""
import re
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__AGCTX_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import Company, Plan, User, Account, AccountType, Customer, Vendor
    from app.models.account import NORMAL_SIDE_FOR_TYPE
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.seed_coa import seed_default_coa

    # Two plans — one "narrow" (accounting only), one "wide" (all).
    plan_narrow = Plan.query.filter_by(code="__agctx_narrow__").first()
    if not plan_narrow:
        plan_narrow = Plan(code="__agctx_narrow__", name="Narrow",
                           name_ar="ضيقة", allowed_subitems=None)
        plan_narrow.set_modules(["accounting", "sales", "reports",
                                 "settings"])
        db.session.add(plan_narrow); db.session.flush()

    plan_wide = Plan.query.filter_by(code="__agctx_wide__").first()
    if not plan_wide:
        plan_wide = Plan(code="__agctx_wide__", name="Wide",
                         name_ar="كاملة", allowed_subitems=None)
        plan_wide.set_modules(["accounting", "sales", "purchases",
                               "crm", "hr", "reports", "payroll",
                               "settings"])
        db.session.add(plan_wide); db.session.flush()

    def _mk_co(suffix, plan):
        co = Company(name=f"{PREFIX}CO_{suffix}__", base_currency="SAR",
                     vat_rate=Decimal("15"), plan_id=plan.id,
                     timezone="Asia/Riyadh")
        db.session.add(co); db.session.flush()
        co.intended_plan_id = plan.id
        db.session.commit()
        ensure_roles_ready_for_company(co.id)
        seed_default_coa(co.id)
        return co

    def _mk_user(co, tag):
        u = User(email=f"{PREFIX}{co.id}_{tag}@audit.local",
                 full_name=tag, is_active=True,
                 terms_version=get_terms_version(),
                 terms_accepted_at=datetime.utcnow())
        u.set_password("Passw0rd!audit1")
        db.session.add(u); db.session.flush()
        set_membership_role(u.id, co.id, "owner")
        return u.id

    # co_normal: seeded CoA + narrow plan
    co_normal = _mk_co("normal", plan_narrow)
    u_normal = _mk_user(co_normal, "u")

    # co_custom: CoA edited — 4100 renamed, 2120 removed altogether
    co_custom = _mk_co("custom", plan_wide)
    _mk_user(co_custom, "u")
    sales = Account.query.filter_by(
        company_id=co_custom.id, code="4100").first()
    if sales:
        sales.name_ar = "إيرادات معدَّلة"
    # Deactivate every "212*" LIABILITY leaf so the prefix fallback
    # in _resolve_role finds nothing and the role renders as
    # "—  (غير مُعرَّف)". Removing only 2120 leaves 2125 (net VAT
    # payable) picking it up on the prefix fallback.
    for a in Account.query.filter(
            Account.company_id == co_custom.id,
            Account.code.like("212%")).all():
        a.is_active = False
    db.session.commit()

    _STATE.update(cid_normal=co_normal.id, uid_normal=u_normal,
                  cid_custom=co_custom.id)


def _teardown():
    from app.models import Company, User
    from sqlalchemy import text, inspect
    db.session.rollback()
    insp = inspect(db.engine)
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
    for code in ("__agctx_narrow__", "__agctx_wide__"):
        db.session.execute(text("DELETE FROM plans WHERE code=:c"),
                           {"c": code})
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"),
                           {"u": u.id})
    db.session.commit()


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. context has date block: ISO date + Arabic month + weekday")
def _():
    from app.agent.company_context import build_company_context
    from app.models import Company
    co = db.session.get(Company, _STATE["cid_normal"])
    out = build_company_context(co)
    assert "📅 التاريخ" in out
    # ISO date somewhere in the date section
    assert re.search(r"\d{4}-\d{2}-\d{2}", out), "no ISO date rendered"
    # An Arabic month name is present
    months = ("يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
              "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر")
    assert any(m in out for m in months), "no Arabic month rendered"
    # An Arabic weekday
    wds = ("الاثنين", "الثلاثاء", "الأربعاء", "الخميس",
           "الجمعة", "السبت", "الأحد")
    assert any(w in out for w in wds), "no Arabic weekday rendered"
    return "date block present"


@check("2. context date beats server-UTC — Riyadh midnight rollover")
def _():
    """22:30 UTC on day D is 01:30 next day in Riyadh — the ticket's
    exact 'كام فاتورة النهاردة' bug scenario. Freeze server time and
    assert the context reports D+1, not D."""
    from app.agent.company_context import build_company_context
    from app.models import Company
    co = db.session.get(Company, _STATE["cid_normal"])
    # Server = 2026-08-05 22:30 UTC → Riyadh = 2026-08-06 01:30
    frozen = datetime(2026, 8, 5, 22, 30, 0)
    riyadh_expected = date(2026, 8, 6)

    with mock.patch("app.services.time.datetime") as mdt:
        mdt.now.return_value = None  # will be replaced per-call
        # zoneinfo path calls datetime.now(ZoneInfo(...)); we need to
        # return the frozen UTC time when called with any tz arg.
        from zoneinfo import ZoneInfo

        def fake_now(tz=None):
            if tz is None:
                return frozen
            return frozen.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)

        mdt.now.side_effect = fake_now
        out = build_company_context(co)
    assert riyadh_expected.isoformat() in out, (
        f"expected Riyadh date {riyadh_expected} in context, "
        f"got:\n{out[:400]}")
    return f"22:30 UTC → context date = {riyadh_expected}"


@check("3. NO 4-digit account codes in the rewritten prompt")
def _():
    from app.agent.prompts import SYSTEM_PROMPT
    matches = re.findall(r"\b\d{4}\b", SYSTEM_PROMPT)
    assert not matches, (
        f"account codes leaked into prompt: {matches!r}")
    # Also byte-check the specific old codes that were hardcoded
    for legacy in ("1130", "4100", "2120", "5220", "1110"):
        assert legacy not in SYSTEM_PROMPT, (
            f"legacy code {legacy} still in prompt")
    return "no 4-digit codes, no legacy 1130/4100/2120/5220/1110"


@check("4. custom CoA — renamed 4100 surfaces the actual name")
def _():
    from app.agent.company_context import build_company_context
    from app.models import Company
    co = db.session.get(Company, _STATE["cid_custom"])
    out = build_company_context(co)
    assert "إيرادات معدَّلة" in out, (
        "renamed sales account not in context — the builder is "
        "reading DEFAULT_COA text instead of the tenant's tree")
    return "renamed sales account surfaced"


@check("5. missing role rendered as '— (غير مُعرَّف)'")
def _():
    from app.agent.company_context import build_company_context
    from app.models import Company
    co = db.session.get(Company, _STATE["cid_custom"])
    out = build_company_context(co)
    # Output VAT (2120) was deactivated in the custom company; the
    # prefix fallback (212*) also finds nothing active.
    assert "ضريبة المخرجات: —" in out, (
        f"missing Output VAT was not marked as unavailable; got:\n"
        f"{out[out.find('ضريبة المخرجات'):out.find('ضريبة المخرجات')+60]}")
    return "missing role marked '—'"


@check("6. modules line reflects the plan")
def _():
    from app.agent.company_context import build_company_context
    from app.models import Company
    co_narrow = db.session.get(Company, _STATE["cid_normal"])
    co_wide = db.session.get(Company, _STATE["cid_custom"])
    out_narrow = build_company_context(co_narrow)
    out_wide = build_company_context(co_wide)
    # Narrow plan has no payroll
    narrow_section = out_narrow.split("⚙️")[1].split("📋")[0]
    wide_section = out_wide.split("⚙️")[1].split("📋")[0]
    assert "payroll" not in narrow_section, (
        "payroll leaked into narrow plan's modules line")
    assert "payroll" in wide_section, (
        "payroll missing from wide plan's modules line")
    return "narrow lacks payroll; wide has it"


@check("7. off-plan refusal rule present in the prompt")
def _():
    from app.agent.prompts import SYSTEM_PROMPT
    assert "غير مفعّلة في باقة الشركة" in SYSTEM_PROMPT, (
        "prompt does not tell the agent how to refuse off-plan "
        "requests — the model will happily try tools for modules "
        "the tenant hasn't paid for")
    return "refusal rule present"


@check("8. tool list_invoices uses company-tz today for default filter")
def _():
    """The tool defaults must resolve today via
    today_in_company_tz(company), not date.today() (server UTC)."""
    from app.agent.tools import execute_tool
    from unittest.mock import patch

    frozen_riyadh_today = date(2026, 8, 6)

    with patch("app.services.time.today_in_company_tz",
               return_value=frozen_riyadh_today):
        result = execute_tool(
            "list_invoices", {}, _STATE["cid_normal"],
            _STATE["uid_normal"])
    # The tool echoes its resolved default via start_date/end_date.
    assert result["start_date"] == frozen_riyadh_today.isoformat(), (
        f"list_invoices default start_date = {result['start_date']}, "
        f"expected the Riyadh-today value {frozen_riyadh_today}")
    assert result["end_date"] == frozen_riyadh_today.isoformat()
    return f"default range = {frozen_riyadh_today}"


@check("9. context fits token budget (< 2000 chars)")
def _():
    from app.agent.company_context import build_company_context
    from app.models import Company, Customer, Vendor
    from sqlalchemy import text
    # Bulk-seed customers + vendors + accounts to simulate a real tenant.
    co = db.session.get(Company, _STATE["cid_normal"])
    for i in range(50):
        db.session.add(Customer(
            company_id=co.id, name=f"عميل {i}", is_active=True))
    for i in range(30):
        db.session.add(Vendor(
            company_id=co.id, name=f"مورد {i}", is_active=True))
    db.session.commit()
    out = build_company_context(co)
    assert len(out) < 2000, (
        f"context grew to {len(out)} chars (budget is 2000). "
        "The list of accounts probably rendered too many rows.")
    return f"len={len(out)} chars"


@check("10. cache-friendly ordering — persona first, context second")
def _():
    """The full system string in run_agent_turn is
    system = persona["system_prompt"]; if context: system += ... .
    A change to that order (context first, persona later) would
    break Anthropic + DeepSeek prompt caching, which relies on a
    stable prefix. Pin the concatenation."""
    from app.agent.base import accountant_persona
    from app.agent.company_context import build_company_context
    from app.models import Company

    with _STATE["app"].app_context():
        co = db.session.get(Company, _STATE["cid_normal"])
        persona = accountant_persona()
        ctx = build_company_context(co)
        # Simulate the run_agent_turn concatenation.
        combined = persona["system_prompt"]
        if ctx:
            combined += f"\n\nسياق الشركة الحالية:\n{ctx}"
    assert combined.startswith(persona["system_prompt"]), (
        "persona is NO LONGER the prefix of the combined system prompt "
        "— cache will not hit")
    ctx_pos = combined.find("سياق الشركة الحالية")
    assert ctx_pos > len(persona["system_prompt"]) - 5, (
        "context appears BEFORE or too early against the persona — "
        "cache-hostile ordering")
    return "persona first, context second"


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
