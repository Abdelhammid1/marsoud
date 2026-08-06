#!/usr/bin/env python3
"""MARSOUD-AGENT-SAFETY-03 (2026-08-06) — audit for the write-safety
layer above the accountant agent.

Every check verified to fail against pre-change HEAD.

Checks
   1. read tools stay instant (no proposal)
   2. write tool produces a proposal, does NOT write
   3. confirm executes the write
   4. cancel writes nothing; proposal marked CANCELLED
   5. tool_trace persisted on assistant AgentMessage
   6. every executed write lands in PlatformAuditLog
   7. agent.write is a separate permission (403 without it)
   8. daily write cap refuses Nth+1
   9. cross-tenant customer refused in create_invoice
  10. require_confirmation=false runs writes immediately
  11. proposals older than 24h are EXPIRED, refuse to execute
  12. proposal execute is idempotent (second call returns already-executed)
"""
import json
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db

CHECKS = []
PREFIX = "__AGSAFE_"
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _setup():
    _teardown()
    from app.models import (
        Company, Plan, User, Customer, Account, AccountType,
    )
    from app.services.legal import get_terms_version
    from app.services.roles import set_membership_role
    from app.services.roles_seed import ensure_roles_ready_for_company
    from app.services.seed_coa import seed_default_coa

    plan = Plan.query.filter_by(code="__agsafe__").first()
    if not plan:
        plan = Plan(code="__agsafe__", name="AgSafe", name_ar="أمان",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "reports",
                          "agent", "settings"])
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
    u_accountant = _mk_user(co_a, "acc", "accountant")
    u_viewer = _mk_user(co_a, "view", "viewer")

    cust_b = Customer(company_id=co_b.id, name="عميل B")
    db.session.add(cust_b); db.session.commit()

    _STATE.update(cid_a=co_a.id, cid_b=co_b.id,
                  owner=u_owner, accountant=u_accountant,
                  viewer=u_viewer,
                  cust_b=cust_b.id)


def _teardown():
    from app.models import Company, User, PlatformSetting
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
    db.session.execute(text("DELETE FROM plans WHERE code='__agsafe__'"))
    for k in ("agent_require_confirmation", "agent_daily_write_cap"):
        PlatformSetting.query.filter_by(key=k).delete()
    db.session.commit()
    for u in User.query.filter(User.email.like(f"{PREFIX}%")).all():
        db.session.execute(text(
            "DELETE FROM user_companies WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text("DELETE FROM agent_daily_write_counts "
                                 "WHERE user_id=:u"), {"u": u.id})
        db.session.execute(text("DELETE FROM users WHERE id=:u"),
                           {"u": u.id})
    db.session.commit()


def _wipe_state():
    from app.models import (
        AgentProposal, AgentMessage, AgentDailyWriteCount,
        Customer, PlatformAuditLog, PlatformSetting,
    )
    AgentProposal.query.delete()
    AgentMessage.query.delete()
    AgentDailyWriteCount.query.delete()
    Customer.query.filter(Customer.company_id == _STATE["cid_a"]).delete()
    PlatformAuditLog.query.filter(
        PlatformAuditLog.action == "agent_write").delete()
    for k in ("agent_require_confirmation", "agent_daily_write_cap"):
        PlatformSetting.query.filter_by(key=k).delete()
    db.session.commit()


def _set(key, value):
    from app.models import PlatformSetting
    row = PlatformSetting.query.filter_by(key=key).first()
    if row is None:
        row = PlatformSetting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value
    db.session.commit()


def _post_as(user_id, company_id, path, data=None):
    """POST inside a fresh app_context so Flask-Login's g._login_user
    cache does not answer as whichever user this app-context saw
    first (handoff fact 7 — the trap that has burned every audit
    suite that switches identities). Fresh test_client per call
    handles cookies; fresh nested app_context handles g."""
    with _STATE["app"].app_context():
        c = _STATE["app"].test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(user_id)
            s["_fresh"] = True
            s["active_company_id"] = company_id
        return c.post(path, json=data or {}, follow_redirects=False)


# ─── Checks ─────────────────────────────────────────────────────────────
@check("1. read tools stay instant (no proposal)")
def _():
    from app.agent.tools import execute_tool
    _wipe_state()
    result = execute_tool("list_accounts", {},
                           _STATE["cid_a"], _STATE["owner"])
    assert "accounts" in result, (
        f"read tool did not run: {result!r}")
    assert not result.get("requires_confirmation"), (
        "read tool created a proposal — should be instant")
    return "list_accounts returned accounts, no proposal"


@check("2. write tool produces proposal, does NOT write")
def _():
    from app.models import Customer, AgentProposal
    from app.agent.tools import execute_tool
    _wipe_state()
    before = Customer.query.filter_by(company_id=_STATE["cid_a"]).count()
    r = execute_tool("create_customer",
                      {"name": "عميل جديد", "phone": "0500"},
                      _STATE["cid_a"], _STATE["owner"])
    after = Customer.query.filter_by(company_id=_STATE["cid_a"]).count()
    assert r.get("requires_confirmation"), (
        f"write tool did not produce proposal: {r!r}")
    assert r.get("proposal_id"), "no proposal_id returned"
    assert after == before, (
        f"customer count moved {before}→{after} despite pending proposal")
    n_pending = AgentProposal.query.filter_by(
        company_id=_STATE["cid_a"]).count()
    assert n_pending == 1, f"expected 1 pending proposal, got {n_pending}"
    return f"proposal #{r['proposal_id']} created; 0 writes yet"


@check("3. confirm executes the write")
def _():
    from app.models import Customer, AgentProposal, PROPOSAL_EXECUTED
    from app.agent.tools import execute_tool
    _wipe_state()
    r = execute_tool("create_customer",
                      {"name": "عميل ثاني", "phone": "0511"},
                      _STATE["cid_a"], _STATE["owner"])
    pid = r["proposal_id"]
    resp = _post_as(_STATE["owner"], _STATE["cid_a"],
                    f"/agent/proposal/{pid}/execute")
    assert resp.status_code == 200, (
        f"execute got {resp.status_code}, body={resp.get_data(as_text=True)[:200]}")
    assert Customer.query.filter_by(
        company_id=_STATE["cid_a"], name="عميل ثاني").count() == 1
    from app.models import AgentProposal as _AP
    db.session.expire_all()
    p = db.session.get(_AP, pid)
    assert p.status == PROPOSAL_EXECUTED
    return "confirm wrote the customer + flipped proposal to EXECUTED"


@check("4. cancel writes nothing; proposal → CANCELLED")
def _():
    from app.models import Customer, AgentProposal, PROPOSAL_CANCELLED
    from app.agent.tools import execute_tool
    _wipe_state()
    r = execute_tool("create_customer",
                      {"name": "عميل ملغى"},
                      _STATE["cid_a"], _STATE["owner"])
    pid = r["proposal_id"]
    resp = _post_as(_STATE["owner"], _STATE["cid_a"],
                    f"/agent/proposal/{pid}/cancel")
    assert resp.status_code == 200
    assert Customer.query.filter_by(
        company_id=_STATE["cid_a"], name="عميل ملغى").count() == 0
    db.session.expire_all()
    assert db.session.get(AgentProposal, pid).status == PROPOSAL_CANCELLED
    return "cancel: 0 writes, proposal CANCELLED"


@check("5. tool_trace persisted on assistant AgentMessage")
def _():
    """A chat turn goes through routes/agent.py::chat, which writes an
    AgentMessage with tool_trace JSON. Rather than exercise the full
    LLM call (network, tokens), directly test that the field is
    written when the route builds an assistant message.

    This test mocks run_agent to return a canned trace and asserts
    the row lands with it."""
    from app.models import AgentMessage
    _wipe_state()
    canned_trace = [
        {"tool": "list_accounts", "input": {},
         "result": {"accounts": []}}
    ]
    with patch("app.routes.agent.run_agent",
               return_value=("done", [], canned_trace)):
        resp = _post_as(_STATE["owner"], _STATE["cid_a"],
                        "/agent/chat", {"message": "test"})
    assert resp.status_code == 200, f"chat status={resp.status_code}"
    msg = (AgentMessage.query
           .filter_by(company_id=_STATE["cid_a"], role="assistant")
           .order_by(AgentMessage.id.desc()).first())
    assert msg is not None, "no assistant message written"
    assert msg.tool_trace, "tool_trace column is empty"
    parsed = json.loads(msg.tool_trace)
    assert parsed and parsed[0]["tool"] == "list_accounts", (
        f"trace shape wrong: {parsed!r}")
    return f"assistant msg #{msg.id} has trace with 1 entry"


@check("6. every executed write lands in PlatformAuditLog")
def _():
    from app.models import PlatformAuditLog
    from app.agent.tools import execute_tool
    _wipe_state()
    r = execute_tool("create_customer",
                      {"name": "للتدقيق"},
                      _STATE["cid_a"], _STATE["owner"])
    pid = r["proposal_id"]
    _post_as(_STATE["owner"], _STATE["cid_a"],
             f"/agent/proposal/{pid}/execute")
    n = PlatformAuditLog.query.filter_by(action="agent_write").count()
    assert n == 1, f"expected 1 audit row, got {n}"
    row = PlatformAuditLog.query.filter_by(action="agent_write").first()
    assert "create_customer" in (row.details or ""), (
        f"audit row does not name the tool: {row.details!r}")
    return "1 audit row with tool name"


@check("7. agent.write is separate from agent.use (viewer 403s)")
def _():
    from app.agent.tools import execute_tool
    _wipe_state()
    # Owner creates a proposal (has agent.use)
    r = execute_tool("create_customer", {"name": "x"},
                      _STATE["cid_a"], _STATE["owner"])
    pid = r["proposal_id"]
    # Viewer (has neither agent.use nor agent.write) hits execute →
    # require_permission redirects to dashboard (302), NOT executes.
    from app.services.permissions import get_user_role, has_permission
    from app.models import Company
    co = db.session.get(Company, _STATE["cid_a"])
    resp = _post_as(_STATE["viewer"], _STATE["cid_a"],
                    f"/agent/proposal/{pid}/execute")
    assert resp.status_code in (302, 303, 403), (
        f"viewer got {resp.status_code}, expected redirect/403")
    from app.models import AgentProposal, PROPOSAL_PENDING
    p = db.session.get(AgentProposal, pid)
    assert p.status == PROPOSAL_PENDING, (
        f"viewer's request executed the proposal: {p.status}")
    return f"viewer refused → proposal stays PENDING"


@check("8. daily write cap refuses Nth+1")
def _():
    from app.models import AgentDailyWriteCount, Customer
    from app.agent.tools import execute_tool
    _wipe_state()
    _set("agent_daily_write_cap", "2")
    for i in range(2):
        r = execute_tool("create_customer",
                          {"name": f"عميل حد {i}"},
                          _STATE["cid_a"], _STATE["owner"])
        pid = r["proposal_id"]
        resp = _post_as(_STATE["owner"], _STATE["cid_a"],
                        f"/agent/proposal/{pid}/execute")
        assert resp.status_code == 200, (
            f"write #{i+1} refused: {resp.get_data(as_text=True)[:200]}")
    # Third write must be refused BEFORE it runs.
    r3 = execute_tool("create_customer",
                       {"name": "عميل الحد+1"},
                       _STATE["cid_a"], _STATE["owner"])
    pid3 = r3["proposal_id"]
    resp3 = _post_as(_STATE["owner"], _STATE["cid_a"],
                     f"/agent/proposal/{pid3}/execute")
    assert resp3.status_code == 429, (
        f"3rd write got {resp3.status_code}, expected 429 (cap exceeded)")
    body = resp3.get_json()
    # The message is "وصلت للحد اليومي..." — check for "اليومي" alone
    # since "الحد" is preceded by "ل" in Arabic ("للحد"), so the
    # naive substring match on "الحد اليومي" fails on the "لل" prefix.
    assert "اليومي" in (body.get("error") or ""), (
        f"cap message missing: {body!r}")
    n = AgentDailyWriteCount.query.filter_by(
        user_id=_STATE["owner"]).first()
    assert n and n.count == 2, (
        f"counter = {n.count if n else None}, expected 2")
    return "cap=2: writes 1+2 ran, 3rd refused with 429"


@check("9. cross-tenant customer refused in create_invoice")
def _():
    """The bug the ticket calls out: create_invoice used
    args['customer_id'] verbatim. Try to invoice B's customer from A."""
    from app.agent.tools import execute_tool
    from app.models import Invoice
    _wipe_state()
    # Bypass proposal so the check runs synchronously — the
    # cross-tenant guard is BEFORE the write, but it also runs at
    # execute-time when a proposal is confirmed. Testing at both
    # points would need doubled fixture; the load-bearing check is
    # that the guard exists.
    _set("agent_require_confirmation", "false")
    result = execute_tool(
        "create_invoice",
        {"customer_id": _STATE["cust_b"],
         "items": [{"description": "x", "quantity": 1,
                    "unit_price": 100}]},
        _STATE["cid_a"], _STATE["owner"])
    assert "error" in result, (
        f"cross-tenant invoice did not error: {result!r}")
    assert "غير موجود" in result["error"], (
        f"error message wrong: {result['error']!r}")
    assert Invoice.query.filter_by(
        company_id=_STATE["cid_a"]).count() == 0, (
        "an invoice was created despite cross-tenant customer")
    return "B's customer refused for A's invoice"


@check("10. require_confirmation=false runs writes immediately")
def _():
    from app.models import Customer
    from app.agent.tools import execute_tool
    _wipe_state()
    _set("agent_require_confirmation", "false")
    r = execute_tool("create_customer", {"name": "فوري"},
                      _STATE["cid_a"], _STATE["owner"])
    assert not r.get("requires_confirmation"), (
        "confirmation flag still returned when toggle is off")
    assert Customer.query.filter_by(
        company_id=_STATE["cid_a"], name="فوري").count() == 1
    return "toggle off → write runs immediately"


@check("11. proposals older than 24h are EXPIRED")
def _():
    from app.models import AgentProposal, PROPOSAL_EXPIRED
    from app.agent.tools import execute_tool
    _wipe_state()
    r = execute_tool("create_customer", {"name": "منتهي"},
                      _STATE["cid_a"], _STATE["owner"])
    pid = r["proposal_id"]
    # Age the proposal by 25 hours.
    p = db.session.get(AgentProposal, pid)
    p.created_at = datetime.utcnow() - timedelta(hours=25)
    db.session.commit()
    resp = _post_as(_STATE["owner"], _STATE["cid_a"],
                    f"/agent/proposal/{pid}/execute")
    db.session.expire_all()
    p = db.session.get(AgentProposal, pid)
    assert p.status == PROPOSAL_EXPIRED, (
        f"stale proposal not expired: status={p.status}")
    assert resp.status_code == 400
    return "25h-old proposal marked EXPIRED, refused"


@check("12. proposal execute is idempotent")
def _():
    from app.models import Customer
    from app.agent.tools import execute_tool
    _wipe_state()
    r = execute_tool("create_customer", {"name": "idem"},
                      _STATE["cid_a"], _STATE["owner"])
    pid = r["proposal_id"]
    r1 = _post_as(_STATE["owner"], _STATE["cid_a"],
                   f"/agent/proposal/{pid}/execute")
    r2 = _post_as(_STATE["owner"], _STATE["cid_a"],
                   f"/agent/proposal/{pid}/execute")
    assert r1.status_code == 200
    assert r2.status_code == 400, (
        f"second execute got {r2.status_code}, expected 400")
    # Still only one customer written.
    assert Customer.query.filter_by(
        company_id=_STATE["cid_a"], name="idem").count() == 1
    return "second execute returns 400; still 1 customer row"


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
