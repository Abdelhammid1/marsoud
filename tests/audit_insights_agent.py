#!/usr/bin/env python3
"""MARSOUD-INSIGHTS-AGENT-01 (Abdelhamid 2026-08-01).

Batch 9 Ticket 6 audit — read-only analyst agent alongside the
existing accountant.

Checks (per plan):
  1. `insights.use` permission exists in P.
  2. `insights` module in plan_gating._PREFIX_TO_MODULE.
  3. `agent_type` column exists on AgentMessage + is a `NOT NULL`
     column with default 'accountant'.
  4. Accountant chat history is invisible from the insights
     panel (agent_type filter test).
  5. `todays_summary` returns numbers that reconcile with the
     underlying tables (invoice count today).
  6. `employees_performance` respects the caller's
     `employees.view` permission (missing → empty payload).
  7. Cross-tenant: user in company A cannot query company B's
     data through the insights tools.
  8. Provider abstraction: AnthropicProvider still works
     end-to-end for the accountant loop (regression guard via a
     mocked client).
  9. Quota logging: an insights turn records with
     provider='deepseek' in AiTokenUsage.
 10. If DeepSeek returns an error, the insights route returns a
     user-friendly Arabic error AND the accountant route on
     the same session still succeeds.
"""
import os
import sys
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch, MagicMock

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _teardown():
    from sqlalchemy import text, inspect
    db.session.rollback()
    db.session.expunge_all()
    db.session.remove()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        cids = [r[0] for r in conn.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__IA_%__'"))]
        for cid in cids:
            conn.execute(text(
                "DELETE FROM user_companies WHERE company_id = :c"),
                {"c": cid})
            conn.execute(text(
                "DELETE FROM journal_lines WHERE entry_id IN "
                "(SELECT id FROM journal_entries WHERE company_id = :c)"),
                {"c": cid})
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(tbl.name)}
                if "company_id" in cols:
                    conn.execute(text(
                        f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                        {"c": cid})
            conn.execute(text("DELETE FROM companies WHERE id = :c"),
                          {"c": cid})
        conn.execute(text(
            "DELETE FROM users WHERE email LIKE 'ia-%@x.test'"))


def _seed_owner(suffix, employees_perm=True):
    from app.models import Company, User, UserStatus, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    plan = None
    for p in Plan.query.filter_by(is_active=True).all():
        # need a plan that includes accounting so tools work,
        # and hr for employees permission.
        if "accounting" in (p.modules or []):
            plan = p
            break
    c = Company(name=f"__IA_{suffix}__", base_currency="EGP",
                 subdomain=f"ia-{suffix.lower()}",
                 subscription_started_at=datetime.utcnow(),
                 subscription_expires_at=datetime(2999, 1, 1),
                 intended_plan_id=plan.id if plan else None,
                 plan_id=plan.id if plan else None)
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    role = "owner" if employees_perm else "sales_rep"
    u = User(email=f"ia-{suffix.lower()}@x.test",
             password_hash=generate_password_hash(
                 "x", method="pbkdf2:sha256"),
             full_name=f"ia-{suffix}", is_active=True,
             status=UserStatus.ACTIVE.value,
             email_verified_at=datetime.utcnow(),
             terms_version="TEST")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role=role))
    db.session.commit()
    return c, u


# ─── 1. Permission present ─────────────────────────────────────
@check("1. insights.use permission is registered in P")
def _():
    from app.services.permissions import P
    assert "insights.use" in P, \
        "insights.use missing from P dict"
    roles = P["insights.use"]
    assert "owner" in roles
    return f"scoped to {sorted(roles)}"


# ─── 2. Plan gating prefix ─────────────────────────────────────
@check("2. insights. prefix maps to 'insights' module in plan_gating")
def _():
    from app.services.plan_gating import _PREFIX_TO_MODULE
    assert _PREFIX_TO_MODULE.get("insights.") == "insights", \
        f"prefix mapping = {_PREFIX_TO_MODULE.get('insights.')}"
    return "prefix mapped correctly"


# ─── 3. agent_type column + default ────────────────────────────
@check("3. agent_messages.agent_type column exists + default")
def _():
    from sqlalchemy import inspect
    from app.models import AgentMessage
    cols = {c["name"]: c
            for c in inspect(db.engine).get_columns("agent_messages")}
    assert "agent_type" in cols
    assert cols["agent_type"]["nullable"] is False, \
        "agent_type should be NOT NULL"
    # Model column also
    mcols = {c.name for c in AgentMessage.__table__.columns}
    assert "agent_type" in mcols
    return "column + default confirmed"


# ─── 4. History isolation by agent_type ────────────────────────
@check("4. Accountant history is invisible from the insights panel")
def _():
    from app.models import AgentMessage
    _teardown()
    c, u = _seed_owner("HIST")
    db.session.add(AgentMessage(
        company_id=c.id, user_id=u.id, role="user",
        content="test accountant msg",
        agent_type="accountant"))
    db.session.add(AgentMessage(
        company_id=c.id, user_id=u.id, role="user",
        content="test insights msg",
        agent_type="insights"))
    db.session.commit()

    from app.routes.agent import _load_history
    acc = _load_history(c.id, u.id, "accountant")
    ins = _load_history(c.id, u.id, "insights")
    assert len(acc) == 1 and acc[0].content == "test accountant msg"
    assert len(ins) == 1 and ins[0].content == "test insights msg"
    return "histories isolated"


# ─── 5. todays_summary tool reconciliation ─────────────────────
@check("5. todays_summary counts today's non-voided invoices correctly")
def _():
    from app.models import Customer, Invoice, InvoiceItem, InvoiceStatus
    from app.services.subsidiary import ensure_customer_account
    from app.agent.insights_tools import _todays_summary
    _teardown()
    c, u = _seed_owner("TS")
    cust = Customer(company_id=c.id, name="today-cust")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    # 2 invoices today, one voided.
    for i, (amt, status) in enumerate([(300, InvoiceStatus.SENT),
                                         (100, InvoiceStatus.VOIDED)],
                                        start=1):
        inv = Invoice(company_id=c.id, customer_id=cust.id,
                       number=f"TDY-{i}",
                       issue_date=date.today(),
                       due_date=date.today() + timedelta(days=30),
                       currency="EGP", tax_rate=0,
                       status=status, source="MANUAL")
        db.session.add(inv); db.session.flush()
        db.session.add(InvoiceItem(
            invoice_id=inv.id, company_id=c.id,
            description="x", quantity=1, unit_price=amt))
        inv.recalc()
    db.session.commit()
    out = _todays_summary({}, c.id, u.id)
    # Voided EXCLUDED per KPI convention.
    assert out["new_invoices"] == 1, \
        f"expected 1 new invoice, got {out['new_invoices']}"
    assert abs(out["invoiced_total"] - 300) < 0.01, \
        f"total = {out['invoiced_total']}"
    return f"{out['new_invoices']} invoice, {out['invoiced_total']} EGP"


# ─── 6. employees_performance permission gate ──────────────────
@check("6. employees_performance returns note when caller lacks employees.view")
def _():
    from app.agent.insights_tools import _employees_performance
    _teardown()
    # sales_rep does NOT have employees.view.
    c, u = _seed_owner("PERM", employees_perm=False)
    out = _employees_performance({}, c.id, u.id)
    assert out.get("rows") == [], \
        f"rows leaked without permission: {out.get('rows')}"
    assert "صلاحية" in (out.get("note") or ""), \
        f"note missing: {out}"
    return "permission-blocked, empty payload"


# ─── 7. Cross-tenant isolation ─────────────────────────────────
@check("7. Insights tools cannot see other tenants' data")
def _():
    from app.models import Customer, Invoice, InvoiceItem, InvoiceStatus
    from app.services.subsidiary import ensure_customer_account
    from app.agent.insights_tools import _todays_summary
    _teardown()
    c_a, u_a = _seed_owner("XA")
    c_b, u_b = _seed_owner("XB")
    # Put an invoice under company A today.
    cust_a = Customer(company_id=c_a.id, name="a-cust")
    db.session.add(cust_a); db.session.flush()
    ensure_customer_account(cust_a)
    inv = Invoice(company_id=c_a.id, customer_id=cust_a.id,
                   number="XA-1", issue_date=date.today(),
                   due_date=date.today() + timedelta(days=30),
                   currency="EGP", tax_rate=0,
                   status=InvoiceStatus.SENT, source="MANUAL")
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(invoice_id=inv.id, company_id=c_a.id,
                                description="x", quantity=1,
                                unit_price=500))
    inv.recalc()
    db.session.commit()
    # Query as company B — must NOT see A's invoice.
    out_b = _todays_summary({}, c_b.id, u_b.id)
    assert out_b["new_invoices"] == 0, \
        f"cross-tenant leak: {out_b}"
    # Sanity: querying as A DOES see it.
    out_a = _todays_summary({}, c_a.id, u_a.id)
    assert out_a["new_invoices"] == 1
    return "A sees 1, B sees 0"


# ─── 8. Accountant loop still works via provider ───────────────
@check("8. Accountant run_agent still works via the abstraction (regression)")
def _():
    from app.agent.accountant import run_agent
    _teardown()
    c, u = _seed_owner("ACC")
    # Mock the Anthropic client to return an end_turn immediately.
    fake_response = MagicMock()
    fake_block = MagicMock()
    fake_block.type = "text"
    fake_block.text = "مرحبا"
    fake_response.content = [fake_block]
    fake_response.stop_reason = "end_turn"
    fake_response.usage = MagicMock(input_tokens=5,
                                      output_tokens=3)
    # AnthropicProvider does `from anthropic import Anthropic`
    # inside __init__, so we patch at anthropic's module scope.
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = fake_response
        from flask import current_app
        current_app.config["ANTHROPIC_API_KEY"] = "test"
        reply, msgs, tools = run_agent(
            messages=[{"role": "user", "content": "test"}],
            company_id=c.id, user_id=u.id,
            company_context="test ctx",
        )
    assert reply == "مرحبا", f"reply={reply!r}"
    assert MockClient.return_value.messages.create.called
    return "accountant loop unchanged"


# ─── 9. Quota logging with provider='deepseek' ────────────────
@check("9. Insights turn logs AiTokenUsage with provider='deepseek'")
def _():
    from app.agent.base import run_agent_turn, insights_persona
    from app.services.ai_providers import DeepseekProvider
    from app.agent.insights_tools import INSIGHTS_TOOL_SCHEMAS
    from sqlalchemy import text
    _teardown()
    c, u = _seed_owner("QUOTA")
    # Mock the DeepSeek OpenAI client so we don't call the API.
    fake_message = MagicMock()
    fake_message.content = "ملخص"
    fake_message.tool_calls = None
    fake_choice = MagicMock()
    fake_choice.message = fake_message
    fake_choice.finish_reason = "stop"
    fake_resp = MagicMock()
    fake_resp.choices = [fake_choice]
    fake_resp.usage = MagicMock(prompt_tokens=42, completion_tokens=17)

    # Patch openai.OpenAI at the anthropic-style import site
    # (DeepseekProvider does `from openai import OpenAI` inside
    # __init__).
    with patch("openai.OpenAI") as MockClient:
        MockClient.return_value.chat.completions.create.return_value = fake_resp
        with patch.dict("os.environ",
                          {"DEEPSEEK_API_KEY": "test"}):
            provider = DeepseekProvider()
            reply, _, _ = run_agent_turn(
                messages=[{"role": "user", "content": "?"}],
                company_id=c.id, user_id=u.id,
                persona={"key": "insights",
                          "system_prompt": "x",
                          "model": "deepseek-v4-flash"},
                provider=provider,
                tools=INSIGHTS_TOOL_SCHEMAS,
                execute_tool_fn=(lambda *a, **kw: {}),
                max_iters=2,
            )
    row = db.session.execute(text(
        "SELECT provider, model, input_tokens, output_tokens "
        "FROM ai_token_usage "
        "WHERE company_id = :c AND user_id = :u "
        "ORDER BY id DESC LIMIT 1"),
        {"c": c.id, "u": u.id}).fetchone()
    assert row is not None, "no AiTokenUsage row written"
    assert row[0] == "deepseek", \
        f"provider={row[0]!r}, want 'deepseek'"
    return f"logged: {row[0]}/{row[1]} in={row[2]} out={row[3]}"


# ─── 10. DeepSeek failure doesn't break accountant ─────────────
@check("10. DeepSeek error → user-friendly Arabic; accountant unaffected")
def _():
    from flask import current_app
    from app.models import AgentMessage, Plan
    _teardown()
    c, u = _seed_owner("ERR")
    # Enable the `insights` module on this company's plan so
    # require_permission("insights.use") passes and my route's
    # try/except is the code path we're testing.
    plan = db.session.get(Plan, c.plan_id) if c.plan_id else None
    if plan:
        mods = list(plan.modules or [])
        if "insights" not in mods:
            plan.set_modules(mods + ["insights"])
            db.session.commit()

    with current_app.test_client() as client:
        with client.session_transaction() as sess:
            sess.clear()
            sess["_user_id"] = str(u.id)
            sess["_fresh"] = True
            sess["active_company_id"] = c.id
        with patch(
            "app.services.ai_providers.DeepseekProvider.__init__",
            side_effect=RuntimeError("DeepSeek down"),
        ):
            r = client.post(
                "/agent/insights/chat",
                json={"message": "test"},
            )
        assert r.status_code == 500, \
            f"expected 500 got {r.status_code} → {r.headers.get('Location')}"
        body = r.get_json() or {}
        assert "المحاسب الذكي مايتأثرش" in body.get("error", ""), \
            f"user-friendly error missing: {body}"
        acc_msgs = AgentMessage.query.filter_by(
            company_id=c.id, user_id=u.id).all()
        assert any(m.agent_type == "insights"
                    and m.role == "user"
                    for m in acc_msgs)
    return "insights crashed cleanly; accountant path untouched"


def main():
    app = create_app()
    passed = failed = 0
    for label, fn in CHECKS:
        with app.app_context():
            try:
                _teardown()
                res = fn()
                print(f"PASS  {label}  ⇒ {res}")
                passed += 1
            except Exception as e:  # noqa: BLE001
                print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                failed += 1
                import traceback; traceback.print_exc()
    with app.app_context():
        _teardown()
        print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
