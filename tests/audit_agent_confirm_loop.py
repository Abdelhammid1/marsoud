#!/usr/bin/env python3
"""MARSOUD-AGENT-CONFIRM-LOOP-01 (2026-09-02) — confirm-and-execute
regression audit for the AI agent's write-proposal flow.

Two bugs, one symptom:

  Bug A (client): the proposal card's button was attached via
  addEventListener but got wiped every time an `innerHTML +=` ran
  on the parent chat box — because innerHTML+= serialises + re-parses
  every sibling, dropping their listeners. Fix: single delegated
  listener on #chat-messages + swap the innerHTML+= sites to
  insertAdjacentHTML. That fix isn't unit-testable without a browser.

  Bug B (server): the LLM has no memory of pending proposals, so a
  text "نفذ" or a conversation restart re-called the write tool →
  a NEW proposal card popped up (visible as a "loop"). Fix: dedup
  guard at the top of create_proposal. That's what this audit
  exercises — six checks.
"""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _boot(prefix):
    from sqlalchemy import text
    from app import db
    from app.models import Company, User, Plan
    from app.models.user import user_companies

    db.session.execute(text(
        "DELETE FROM agent_proposals WHERE company_id IN "
        "(SELECT id FROM companies WHERE name LIKE :p)"),
        {"p": f"__{prefix}__%"})
    db.session.execute(text(
        "DELETE FROM companies WHERE name LIKE :p"),
        {"p": f"__{prefix}__%"})
    db.session.execute(text(
        "DELETE FROM users WHERE email LIKE :p"),
        {"p": f"%__{prefix.lower()}__%"})
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C")
        db.session.add(plan)
    plan.set_modules(["accounting"])
    db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"
    users = []
    for i in range(2):
        u = User(email=f"u{i}__{prefix.lower()}__@x.io",
                 full_name=f"U{i}", is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv,
                 terms_accepted_at=datetime.utcnow())
        u.set_password("pw12345678")
        db.session.add(u); db.session.commit()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role="owner"))
        users.append(u.id)
    db.session.commit()
    return c.id, users


@check("1. create_proposal inserts a fresh row on first call")
def _():
    from app import create_app, db
    from app.models import AgentProposal
    from app.services.agent_safety import create_proposal
    app = create_app()
    with app.app_context():
        cid, users = _boot("APL1")
        before = AgentProposal.query.filter_by(company_id=cid).count()
        r = create_proposal(
            tool_name="create_journal_entry",
            args={"description": "test", "lines": [{"a": 1}]},
            company_id=cid, user_id=users[0],
            summary_ar="test summary")
        assert r["requires_confirmation"] is True
        assert r["proposal_id"]
        after = AgentProposal.query.filter_by(company_id=cid).count()
        assert after == before + 1
        return f"proposal_id={r['proposal_id']}"


@check("2. duplicate call returns SAME proposal_id (no second row)")
def _():
    from app import create_app, db
    from app.models import AgentProposal
    from app.services.agent_safety import create_proposal
    app = create_app()
    with app.app_context():
        cid, users = _boot("APL2")
        r1 = create_proposal(
            tool_name="create_journal_entry",
            args={"description": "dup", "lines": [{"x": 1}]},
            company_id=cid, user_id=users[0],
            summary_ar="s")
        n1 = AgentProposal.query.filter_by(company_id=cid).count()
        r2 = create_proposal(
            tool_name="create_journal_entry",
            args={"description": "dup", "lines": [{"x": 1}]},
            company_id=cid, user_id=users[0],
            summary_ar="s")
        n2 = AgentProposal.query.filter_by(company_id=cid).count()
        assert r1["proposal_id"] == r2["proposal_id"], \
            f"got {r1['proposal_id']} vs {r2['proposal_id']}"
        assert n1 == n2, f"{n1} → {n2} (dedup failed)"
        return f"same id {r1['proposal_id']}, count stable at {n2}"


@check("3. dedup ignores the _confirmed_proposal_id marker")
def _():
    from app import create_app, db
    from app.services.agent_safety import create_proposal
    app = create_app()
    with app.app_context():
        cid, users = _boot("APL3")
        base_args = {"description": "same", "lines": [{"x": 1}]}
        r1 = create_proposal(
            tool_name="create_journal_entry",
            args=base_args, company_id=cid, user_id=users[0])
        # Same intent, but with the internal marker set (which
        # execute_proposal would normally pass through).
        with_marker = dict(base_args)
        with_marker["_confirmed_proposal_id"] = 999
        r2 = create_proposal(
            tool_name="create_journal_entry",
            args=with_marker, company_id=cid, user_id=users[0])
        assert r1["proposal_id"] == r2["proposal_id"]
        return "marker-only difference correctly deduped"


@check("4. dedup does NOT collide across users")
def _():
    from app import create_app, db
    from app.services.agent_safety import create_proposal
    app = create_app()
    with app.app_context():
        cid, users = _boot("APL4")
        args = {"description": "same-args-different-user",
                "lines": [{"x": 1}]}
        r1 = create_proposal(
            tool_name="create_journal_entry",
            args=args, company_id=cid, user_id=users[0])
        r2 = create_proposal(
            tool_name="create_journal_entry",
            args=args, company_id=cid, user_id=users[1])
        assert r1["proposal_id"] != r2["proposal_id"], \
            "cross-user leakage!"
        return f"user0={r1['proposal_id']}, user1={r2['proposal_id']}"


@check("5. cancelled / executed proposals do NOT count for dedup")
def _():
    from app import create_app, db
    from app.models import (
        AgentProposal, PROPOSAL_CANCELLED, PROPOSAL_EXECUTED,
    )
    from app.services.agent_safety import create_proposal
    app = create_app()
    with app.app_context():
        cid, users = _boot("APL5")
        args = {"description": "post-cancel", "lines": [{"x": 1}]}
        r1 = create_proposal(
            tool_name="create_journal_entry",
            args=args, company_id=cid, user_id=users[0])
        # Cancel r1 → user re-asks for the same thing → NEW proposal
        p1 = db.session.get(AgentProposal, r1["proposal_id"])
        p1.status = PROPOSAL_CANCELLED
        db.session.commit()
        r2 = create_proposal(
            tool_name="create_journal_entry",
            args=args, company_id=cid, user_id=users[0])
        assert r2["proposal_id"] != r1["proposal_id"], \
            "cancelled proposal was resurrected!"
        # Also test EXECUTED
        p2 = db.session.get(AgentProposal, r2["proposal_id"])
        p2.status = PROPOSAL_EXECUTED
        db.session.commit()
        r3 = create_proposal(
            tool_name="create_journal_entry",
            args=args, company_id=cid, user_id=users[0])
        assert r3["proposal_id"] not in (r1["proposal_id"],
                                          r2["proposal_id"])
        return "cancelled + executed both correctly bypassed"


@check("6. dedup bounded to 24h — stale PENDING is ignored")
def _():
    from app import create_app, db
    from app.models import AgentProposal
    from app.services.agent_safety import create_proposal
    app = create_app()
    with app.app_context():
        cid, users = _boot("APL6")
        args = {"description": "stale", "lines": [{"x": 1}]}
        r1 = create_proposal(
            tool_name="create_journal_entry",
            args=args, company_id=cid, user_id=users[0])
        # Age r1 past 24h
        p1 = db.session.get(AgentProposal, r1["proposal_id"])
        p1.created_at = datetime.utcnow() - timedelta(hours=25)
        db.session.commit()
        r2 = create_proposal(
            tool_name="create_journal_entry",
            args=args, company_id=cid, user_id=users[0])
        assert r2["proposal_id"] != r1["proposal_id"], \
            "stale proposal reused past its 24h window"
        return "stale PENDING correctly ignored"


def main():
    passed = failed = 0
    for label, fn in CHECKS:
        try:
            res = fn()
            print(f"PASS  {label}  ⇒ {res}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
            failed += 1
            import traceback; traceback.print_exc()
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
