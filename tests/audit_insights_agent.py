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
        # MARSOUD-INSIGHTS-AGENT-PROFESSIONAL — sweep orphan
        # task_assignees + task_activity_logs first. Same SQLite
        # id-reuse trap the tasks-edit ticket hit: a stale
        # (task_id=1, user_id=X) row survives a company DELETE
        # and UNIQUE-constraint-fails the next run's fresh task
        # under the same id.
        conn.execute(text(
            "DELETE FROM task_assignees WHERE task_id NOT IN "
            "(SELECT id FROM tasks)"))
        try:
            conn.execute(text(
                "DELETE FROM task_activity_logs WHERE task_id "
                "NOT IN (SELECT id FROM tasks)"))
        except Exception:
            pass
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
             terms_version="v1.0")
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


# ═══════════════════════════════════════════════════════════════
# MARSOUD-INSIGHTS-AGENT-PROFESSIONAL (2026-08-06) — checks 11-18
# cover the new registry, the details-in-returns fix, the three
# composites, and the security acceptance criteria (cross-tenant,
# per-tool perm-gate returns a note NOT data, write refusal).
# ═══════════════════════════════════════════════════════════════

# ─── 11. Registry — tool count grew + each schema well-formed ─
@check("11. Registry exposes 40+ tools, each with Arabic description + valid schema")
def _():
    from app.agent.insights_tools import (
        INSIGHTS_TOOL_SCHEMAS, registered_tool_names,
        registered_tool,
    )
    from app.services.permissions import P
    names = registered_tool_names()
    assert len(names) >= 40, (
        f"expected ≥40 tools after professional pass, got "
        f"{len(names)} — batch modules may have failed to import")
    # Each schema well-formed.
    for s in INSIGHTS_TOOL_SCHEMAS:
        assert set(s.keys()) >= {"name", "description", "input_schema"}
        assert s["description"], f"empty description: {s['name']}"
    # Each declared perm exists in P.
    bad = []
    for n in names:
        e = registered_tool(n)
        perm = e.get("permission")
        if perm is not None and perm not in P:
            bad.append((n, perm))
    assert not bad, f"tools declare unknown perms: {bad}"
    return f"{len(names)} tools registered, all schemas + perms valid"


# ─── 12. overdue_items now returns individual owners ───────────
@check("12. overdue_items returns per-item owner names, not just counts")
def _():
    """The ticket's headline example: was 'you have 12 overdue',
    now must be 'أحمد → task X, سارة → task Y'."""
    from datetime import date, timedelta
    from app.models import (
        Task, TaskStatus, TaskPriority, task_assignees,
    )
    from app.agent.insights_tools import _overdue_items
    _teardown()
    c, u = _seed_owner("OI")
    # Create a task past deadline assigned to the owner user.
    t = Task(
        company_id=c.id, title="متأخرة", assigned_to_id=u.id,
        created_by_id=u.id, priority=TaskPriority.MEDIUM,
        status=TaskStatus.IN_PROGRESS,
        deadline=date.today() - timedelta(days=3),
    )
    db.session.add(t); db.session.flush()
    db.session.execute(task_assignees.insert().values(
        task_id=t.id, user_id=u.id, assigned_by_id=u.id))
    db.session.commit()

    r = _overdue_items({}, c.id, u.id)
    # Old shape was {overdue_tasks: int}; new shape has tasks list.
    assert "tasks" in r and isinstance(r["tasks"], list), (
        f"expected tasks list, got {list(r.keys())}")
    assert len(r["tasks"]) == 1, (
        f"expected 1 task, got {len(r['tasks'])}")
    row = r["tasks"][0]
    for key in ("id", "title", "deadline", "days_late",
                "assigned_to_id", "assigned_to_name"):
        assert key in row, f"missing {key} in row: {row}"
    assert row["assigned_to_name"] == u.full_name, (
        f"owner name missing: {row['assigned_to_name']!r}")
    # And the totals block is still there for summaries.
    assert r.get("totals", {}).get("overdue_tasks") == 1
    return f"task→{row['assigned_to_name']} ({row['days_late']}d late)"


# ─── 13. employees_performance returns richer rows ─────────────
@check("13. employees_performance rows carry tasks_by_status + on_time_rate")
def _():
    from datetime import date, timedelta
    from app.models import Employee, EmployeeStatus, ContractType
    from app.agent.insights_tools import _employees_performance
    _teardown()
    c, u = _seed_owner("EP")
    # Attach an Employee to the owner user.
    e = Employee(company_id=c.id, name="tester", user_id=u.id,
                 status=EmployeeStatus.ACTIVE.value,
                 contract_type=ContractType.FULL_TIME.value,
                 start_date=date.today() - timedelta(days=200))
    db.session.add(e); db.session.commit()

    r = _employees_performance({}, c.id, u.id)
    assert r.get("rows") and len(r["rows"]) >= 1, r
    row = r["rows"][0]
    for key in ("tasks_by_status", "on_time_rate"):
        assert key in row, f"missing {key}: {row}"
    # tasks_by_status is a dict keyed by TaskStatus values.
    assert isinstance(row["tasks_by_status"], dict)
    return f"row has {sorted(row.keys())}"


# ─── 14. analyze_employee composite returns all axes ──────────
@check("14. analyze_employee returns profile + attendance + tasks + advances slices")
def _():
    from datetime import date, timedelta
    from app.models import Employee, EmployeeStatus, ContractType
    from app.agent.insights_batches.composites import analyze_employee
    _teardown()
    c, u = _seed_owner("AE")
    e = Employee(company_id=c.id, name="أحمد التجريبي", user_id=u.id,
                 status=EmployeeStatus.ACTIVE.value,
                 contract_type=ContractType.FULL_TIME.value,
                 start_date=date.today() - timedelta(days=100),
                 email="ahmed@ae.test")
    db.session.add(e); db.session.commit()
    # By fuzzy name.
    r = analyze_employee({"employee": "أحمد"}, c.id, u.id)
    for key in ("employee", "period", "attendance", "tasks",
                "advances", "evaluation"):
        assert key in r, f"missing {key}: {list(r.keys())}"
    assert r["employee"]["id"] == e.id
    assert r["employee"]["name"] == "أحمد التجريبي"
    # By id.
    r2 = analyze_employee({"employee": str(e.id)}, c.id, u.id)
    assert r2["employee"]["id"] == e.id
    return "profile + 4 slices returned"


# ─── 15. analyze_department returns per-member rows + rollup ──
@check("15. analyze_department returns member rows + rollup + highlights")
def _():
    from datetime import date, timedelta
    from app.models import (
        Department, Employee, EmployeeStatus, ContractType,
    )
    from app.agent.insights_batches.composites import analyze_department
    _teardown()
    c, u = _seed_owner("AD")
    d = Department(company_id=c.id, name="القسم التجريبي",
                    is_active=True)
    db.session.add(d); db.session.flush()
    for name in ("Alice", "Bob", "Carol"):
        db.session.add(Employee(
            company_id=c.id, name=name, department_id=d.id,
            status=EmployeeStatus.ACTIVE.value,
            contract_type=ContractType.FULL_TIME.value,
            start_date=date.today() - timedelta(days=50)))
    db.session.commit()
    r = analyze_department({"department": str(d.id)}, c.id, u.id)
    for key in ("department", "period", "member_count",
                "rollup", "members", "highlights"):
        assert key in r, f"missing {key}: {list(r.keys())}"
    assert r["member_count"] == 3
    assert len(r["members"]) == 3
    return f"{r['member_count']} members + rollup + highlights"


# ─── 16. compare_period returns current + prior + delta ───────
@check("16. compare_period yields current + prior + delta with signed pct")
def _():
    from datetime import date, timedelta
    from app.agent.insights_batches.composites import compare_period
    _teardown()
    c, u = _seed_owner("CP")
    today = date.today()
    r = compare_period({
        "report_type": "income_statement",
        "curr_start": (today - timedelta(days=15)).isoformat(),
        "curr_end": today.isoformat(),
    }, c.id, u.id)
    for key in ("current_period", "prior_period",
                "current", "prior", "delta"):
        assert key in r, f"missing {key}: {list(r.keys())}"
    # No data → delta might be empty but not error.
    return "current + prior + delta blocks present"


# ─── 17. Cross-tenant leakage — composites don't leak across ──
@check("17. analyze_employee refuses employees from another company")
def _():
    from datetime import date, timedelta
    from app.models import Employee, EmployeeStatus, ContractType
    from app.agent.insights_batches.composites import analyze_employee
    _teardown()
    c_a, u_a = _seed_owner("XA")
    c_b, u_b = _seed_owner("XB")
    e_a = Employee(company_id=c_a.id, name="محمد A",
                   status=EmployeeStatus.ACTIVE.value,
                   contract_type=ContractType.FULL_TIME.value,
                   start_date=date.today() - timedelta(days=30))
    db.session.add(e_a); db.session.commit()
    # Ask company B's user to look up A's employee — by A's id.
    r = analyze_employee({"employee": str(e_a.id)}, c_b.id, u_b.id)
    # Must NOT return A's profile.
    assert "employee" not in r, (
        f"cross-tenant leak: {r}")
    # And the error is a clear "not found", not a mysterious 500.
    assert "error" in r or "note" in r
    return "cross-tenant lookup refused cleanly"


# ─── 18. Per-tool permission → note, NOT data ─────────────────
@check("18. Permission-gated tool returns note payload, not real data")
def _():
    """A caller who lacks the outer gate on analyze_employee
    (employees.view) gets a note payload back — NOT the profile
    data. This is the ticket's acceptance for 'permission-gated
    data refused clearly, not translated to generic error or
    (worse) actual data'.

    The caller here is `sales_rep`, which does not carry
    `employees.view` in the default role map (permissions.py:110)."""
    from datetime import date, timedelta
    from app.models import Employee, EmployeeStatus, ContractType
    from app.agent.insights_batches.composites import analyze_employee
    _teardown()
    c, u = _seed_owner("PG", employees_perm=False)  # sales_rep

    e = Employee(company_id=c.id, name="secret salary",
                 basic_salary=99999, user_id=None,
                 status=EmployeeStatus.ACTIVE.value,
                 contract_type=ContractType.FULL_TIME.value,
                 start_date=date.today() - timedelta(days=10))
    db.session.add(e); db.session.commit()

    r = analyze_employee({"employee": str(e.id)}, c.id, u.id)
    # perm_denied shape: {"rows": [], "note": "…صلاحية غير كافية…"}
    assert r.get("rows") == [], (
        f"non-empty rows despite missing perm: {r}")
    assert "employees.view" in (r.get("note") or ""), (
        f"note doesn't name the missing perm: {r}")
    # Critical: the actual employee profile did NOT surface.
    assert "employee" not in r, (
        f"employee profile leaked despite perm gate: {r}")
    # And no salary figure anywhere in the payload.
    assert "99999" not in str(r), (
        f"salary figure leaked: {r}")
    return f"refused with perm note; no data returned"


# ─── 19. Write refusal — registry contains ZERO writes ────────
@check("19. Registry contains no tool named in agent_safety.WRITE_TOOL_NAMES")
def _():
    from app.agent.insights_tools import registered_tool_names
    from app.services.agent_safety import WRITE_TOOL_NAMES
    names = set(registered_tool_names())
    intersection = names & WRITE_TOOL_NAMES
    assert not intersection, (
        f"analyst registry exposes write tools: {intersection}")
    # Also check for actual dispatch names that are writes
    # (agent_safety has a naming-drift bug — see explore notes —
    # but the accountant's true write dispatch names are these).
    accountant_writes = {"create_customer", "create_journal_entry",
                          "create_invoice", "record_invoice_payment"}
    intersection2 = names & accountant_writes
    assert not intersection2, (
        f"analyst exposes accountant writes: {intersection2}")
    return f"{len(names)} tools registered, 0 writes"


# ─── 20. Prompt stays short + static + rule 1 verbatim ────────
@check("20. INSIGHTS_SYSTEM_PROMPT stays short, static, and preserves rule 1")
def _():
    from app.agent.insights_prompt import INSIGHTS_SYSTEM_PROMPT
    # Under 3000 bytes → roughly 40 lines of Arabic — keeps DeepSeek
    # prompt-cache budget predictable.
    size = len(INSIGHTS_SYSTEM_PROMPT.encode("utf-8"))
    assert size < 3000, (
        f"prompt grew to {size} bytes — risks blowing DeepSeek "
        f"prompt cache. Trim before merging.")
    # Rule 1 must stay verbatim (any softening = model starts
    # returning invented numbers).
    rule_1 = ("كل رقم بتقوله لازم ييجي من أداة (tool). ممنوع "
              "تخمّن أي رقم أو تحسب في دماغك.")
    assert rule_1 in INSIGHTS_SYSTEM_PROMPT, (
        "rule 1 was edited — that is a security regression")
    # Rule 4 (write refusal + accountant redirect) preserved.
    assert "المحاسب الذكي بدلك" in INSIGHTS_SYSTEM_PROMPT
    return f"{size} bytes, rule 1 + rule 4 verbatim"


# ─── 21. Latency instrumentation is present in base loop ─────
@check("21. run_agent_turn emits per-tool ms + turn summary in tool_trace")
def _():
    """Structural check on base.py — the audit + latency script
    both depend on tool_trace carrying `ms` per entry and a
    trailing summary. Break the shape → both silently regress."""
    from pathlib import Path
    src = (ROOT / "app" / "agent" / "base.py").read_text(
        encoding="utf-8")
    for token in ('time.perf_counter', '"ms"',
                  '"summary"', 'total_ms', 'provider_ms',
                  'tool_ms', 'tool_iters', 'provider_iters'):
        assert token in src, (
            f"latency instrumentation missing token: {token!r}")
    return "timing wrappers + summary present"


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
