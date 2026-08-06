"""MARSOUD-AGENT-SAFETY-03 (2026-08-06) — the layer above the write
tools.

Four responsibilities:

  1. create_proposal(...) — stashes a WRITE tool invocation as a
     PENDING AgentProposal and returns a summary dict the chat UI
     renders as a confirm/cancel card.
  2. execute_proposal(...) — flips a proposal to EXECUTED and runs
     the actual tool. Refuses cancelled / already-executed / older-
     than-24h proposals.
  3. cancel_proposal(...) — user clicks the X.
  4. check_and_increment_write_cap(...) — per-user daily counter of
     writes. Refuses the Nth+1 write for the day (day in company-tz).

Split into a service module so both routes/agent.py AND the tools
module can call it without a circular import. The tools module
imports this from inside execute_tool (lazy), which keeps the
existing tests that mount tools without booting the rest of the app
still working.
"""
import json
from datetime import datetime, timedelta
from app import db


WRITE_TOOL_NAMES = frozenset({
    "create_customer",
    "create_journal_entry",
    "create_invoice",
    "record_payment",
})


class AgentSafetyError(Exception):
    """User-facing safety violation (cap exceeded, expired proposal)."""


# ─── Settings ───────────────────────────────────────────────────────────
def require_confirmation_enabled():
    """Read the PlatformSetting toggle. Default TRUE — confirmation is
    on unless a super-admin turned it off."""
    from app.models.platform_setting import PlatformSetting
    row = PlatformSetting.query.filter_by(
        key="agent_require_confirmation").first()
    if row is None:
        return True
    val = (row.value or "").strip().lower()
    return val not in ("false", "0", "off", "no")


def daily_write_cap():
    """Integer PlatformSetting. Default 20."""
    from app.models.platform_setting import PlatformSetting
    row = PlatformSetting.query.filter_by(
        key="agent_daily_write_cap").first()
    if row is None:
        return 20
    try:
        n = int((row.value or "").strip())
    except (TypeError, ValueError):
        return 20
    return max(0, n)


# ─── Proposal lifecycle ─────────────────────────────────────────────────
def create_proposal(*, tool_name, args, company_id, user_id,
                    summary_ar=None, amount_readable=None):
    """Persist a PENDING proposal. Returns the tool-result dict the
    agent turn appends to its trace."""
    from app.models import AgentProposal, PROPOSAL_PENDING
    p = AgentProposal(
        company_id=company_id, user_id=user_id,
        tool_name=tool_name,
        input_json=json.dumps(args, ensure_ascii=False, default=str),
        summary_ar=summary_ar or "",
        amount_readable=amount_readable or "",
        status=PROPOSAL_PENDING,
    )
    db.session.add(p)
    db.session.commit()
    return {
        "requires_confirmation": True,
        "proposal_id": p.id,
        "tool": tool_name,
        "summary_ar": p.summary_ar,
        "amount_readable": p.amount_readable,
    }


def execute_proposal(proposal_id, *, actor_user_id, active_company_id):
    """Run the tool this proposal represents. Refuses if:
      · proposal is not PENDING (already executed, cancelled, expired)
      · proposal is older than 24h → mark EXPIRED before refusing
      · proposal belongs to a different company than the caller's active

    Returns (result_dict, http_status). On success 200; on refusal 400
    with a specific Arabic message the caller can flash."""
    from app.models import (
        AgentProposal, PROPOSAL_PENDING, PROPOSAL_EXECUTED,
        PROPOSAL_EXPIRED, PROPOSAL_CANCELLED,
    )
    from app.agent.tools import execute_tool

    p = db.session.get(AgentProposal, proposal_id)
    if p is None:
        return {"error": "الاقتراح غير موجود"}, 404
    if p.company_id != active_company_id:
        # Same shape as every other cross-tenant refuse in this codebase.
        return {"error": "الاقتراح تابع لشركة أخرى"}, 403

    age = datetime.utcnow() - p.created_at
    if age > timedelta(hours=24) and p.status == PROPOSAL_PENDING:
        p.status = PROPOSAL_EXPIRED
        db.session.commit()

    if p.status != PROPOSAL_PENDING:
        # Idempotency: two clicks on the same button just get the same
        # answer, no second execute.
        return {
            "error": f"الاقتراح ليس قيد الانتظار — الحالة {p.status}",
            "status": p.status,
        }, 400

    # Cap check runs BEFORE executing. If the user has exhausted their
    # daily writes, the proposal stays PENDING and can be run tomorrow.
    try:
        check_and_increment_write_cap(actor_user_id, active_company_id)
    except AgentSafetyError as e:
        return {"error": str(e)}, 429

    args = json.loads(p.input_json)
    args["_confirmed_proposal_id"] = p.id   # tells execute_tool to
                                             # skip the propose-branch
    result = execute_tool(
        p.tool_name, args, active_company_id, actor_user_id)

    p.status = PROPOSAL_EXECUTED
    p.executed_at = datetime.utcnow()
    p.result_json = json.dumps(result, ensure_ascii=False, default=str)
    db.session.commit()

    # Platform audit trail — the ticket calls this out as the record
    # HR/audit reviews later. log_platform_action commits internally.
    from app.services.superadmin import log_platform_action
    trimmed = json.dumps(args, ensure_ascii=False,
                          default=str)[:300]
    log_platform_action(
        "agent_write",
        actor_id=actor_user_id,
        target_company_id=active_company_id,
        details=f"tool={p.tool_name} input={trimmed}")
    return {"ok": True, "result": result}, 200


def cancel_proposal(proposal_id, *, actor_user_id, active_company_id):
    from app.models import (
        AgentProposal, PROPOSAL_PENDING, PROPOSAL_CANCELLED,
    )
    p = db.session.get(AgentProposal, proposal_id)
    if p is None:
        return {"error": "الاقتراح غير موجود"}, 404
    if p.company_id != active_company_id:
        return {"error": "الاقتراح تابع لشركة أخرى"}, 403
    if p.status != PROPOSAL_PENDING:
        # No-op cancel is fine; return the current status.
        return {"ok": True, "status": p.status}, 200
    p.status = PROPOSAL_CANCELLED
    db.session.commit()
    return {"ok": True, "status": PROPOSAL_CANCELLED}, 200


# ─── Daily write cap ────────────────────────────────────────────────────
def check_and_increment_write_cap(user_id, company_id):
    """Read today's counter (company-tz), refuse if >= cap, otherwise
    upsert-increment. Called from execute_proposal RIGHT BEFORE the
    tool runs."""
    from app.models import AgentDailyWriteCount, Company
    from app.services.time import today_in_company_tz

    company = db.session.get(Company, company_id) if company_id else None
    day = today_in_company_tz(company)
    cap = daily_write_cap()

    row = AgentDailyWriteCount.query.filter_by(
        user_id=user_id, day=day).first()
    current = row.count if row else 0
    if cap > 0 and current >= cap:
        raise AgentSafetyError(
            f"وصلت للحد اليومي لعمليات الكتابة عبر المحاسب الذكي ({cap})")

    if row is None:
        row = AgentDailyWriteCount(user_id=user_id, day=day, count=1)
        db.session.add(row)
    else:
        row.count = current + 1
    db.session.commit()
    return row.count


# ─── Summary generator for the confirm card ────────────────────────────
def summarize_write_call(tool_name, args, company):
    """Best-effort human-readable summary for the confirm card.

    The tool-specific summarizers below stay small — the value comes
    from turning account IDs into account names, and printing amounts
    with the currency. If args are malformed, fall back to the tool
    name + JSON dump so the user still sees SOMETHING.
    """
    from app.models import Account, Customer
    try:
        if tool_name == "create_customer":
            name = (args.get("name") or "").strip()
            return (f"إنشاء عميل جديد: «{name}»", "")
        if tool_name == "create_journal_entry":
            desc = (args.get("description") or "").strip()
            lines = args.get("lines") or []
            total_dr = sum(float(l.get("debit") or 0) for l in lines)
            names = []
            for l in lines[:6]:
                acc = db.session.get(Account, l.get("account_id"))
                acc_name = (acc.name_ar if acc else None) or (
                    acc.name if acc else "?")
                side = "مدين" if float(l.get("debit") or 0) > 0 else "دائن"
                amt = float(l.get("debit") or 0) or float(l.get("credit") or 0)
                names.append(f"{side} {amt:,.2f} — {acc_name}")
            summary = f"قيد جديد: {desc or '(بدون وصف)'}\n" + "\n".join(names)
            return summary, f"{total_dr:,.2f} {company.base_currency}"
        if tool_name == "create_invoice":
            cust = db.session.get(Customer, args.get("customer_id"))
            cust_name = cust.name if cust else "?"
            items = args.get("items") or []
            total = sum(float(i.get("quantity") or 0)
                         * float(i.get("unit_price") or 0)
                         for i in items)
            return (f"فاتورة جديدة للعميل «{cust_name}» — "
                    f"{len(items)} بند",
                    f"{total:,.2f} {company.base_currency}")
        if tool_name == "record_payment":
            amt = float(args.get("amount") or 0)
            return (f"تسجيل دفعة على فاتورة #{args.get('invoice_id')}",
                    f"{amt:,.2f} {company.base_currency}")
    except Exception:
        pass
    return (f"عملية {tool_name}",
            json.dumps(args, ensure_ascii=False, default=str)[:120])
