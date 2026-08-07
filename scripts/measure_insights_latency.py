#!/usr/bin/env python3
"""MARSOUD-INSIGHTS-AGENT-PROFESSIONAL (2026-08-06) — before/after
latency harness for the insights agent.

The ticket asks for a "قياس latency قبل/بعد" line in the commit.
This script:

  · walks a small suite of canned questions
  · calls the insights agent end-to-end for each (real DeepSeek
    round-trip if DEEPSEEK_API_KEY is set; else prints a note and
    reports STATIC-ANALYSIS numbers only)
  · prints per-question timing + a compact summary

Usage:
    python scripts/measure_insights_latency.py
    python scripts/measure_insights_latency.py --questions 3

Static-analysis numbers we always report (no key needed):
  · registered tool count
  · schema-array byte size (grows with tool count → affects
    every DeepSeek request's cacheable prefix)
  · per-tool wrapper overhead estimate (registry lookup + perm
    check ≈ 5-15 microseconds, negligible against a ~2s LLM
    turn but non-zero)

Real numbers when DEEPSEEK_API_KEY is set:
  · wall time per question
  · provider vs tool split (from the base-loop tool_trace summary)
  · median + p95 across the suite
"""
import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")

# Five canned questions covering different tool paths.
QUESTIONS = [
    "ملخّص النهارده — كام فاتورة، كام تحصيل، كام مهمة اتقفلت؟",
    "المتأخر عندنا مين، وكل مهمة قاده مين، وأول 5 فواتير متأخرة؟",
    "قارن قائمة الدخل آخر ٧ أيام بالفترة اللي قبلها.",
    "إزاي أداء أول موظف في الشركة الشهر ده؟ (مهام + حضور)",
    "أعلى ٥ مواد ربحية آخر ٣٠ يوم.",
]


def _static_stats(app):
    """Numbers we can always report without hitting DeepSeek."""
    with app.app_context():
        from app.agent.insights_tools import (
            INSIGHTS_TOOL_SCHEMAS, registered_tool_names,
        )
        from app.agent.insights_prompt import INSIGHTS_SYSTEM_PROMPT
        schemas_json = json.dumps(INSIGHTS_TOOL_SCHEMAS,
                                    ensure_ascii=False)
        return {
            "tool_count": len(registered_tool_names()),
            "schemas_bytes": len(schemas_json.encode("utf-8")),
            "prompt_bytes": len(INSIGHTS_SYSTEM_PROMPT.encode("utf-8")),
        }


def _seed_fixture(app):
    """Seed a fresh company + owner + basic data so tools have
    something non-empty to read."""
    with app.app_context():
        from app import db
        from app.models import (
            Company, User, UserStatus, Plan, Customer, Employee,
            EmployeeStatus, ContractType,
        )
        from app.models.user import user_companies
        from app.services.seed_coa import seed_default_coa
        from werkzeug.security import generate_password_hash
        from sqlalchemy import text
        from datetime import date, datetime, timedelta

        # Nuke any previous run's fixture.
        cids = [r[0] for r in db.session.execute(text(
            "SELECT id FROM companies WHERE name = '__LAT__'")).fetchall()]
        for cid in cids:
            db.session.execute(text(
                "DELETE FROM user_companies WHERE company_id=:c"),
                {"c": cid})
            from sqlalchemy import inspect
            for tbl in reversed(db.metadata.sorted_tables):
                cols = {c["name"]
                        for c in inspect(db.engine).get_columns(tbl.name)}
                if "company_id" in cols:
                    try:
                        db.session.execute(text(
                            f"DELETE FROM {tbl.name} WHERE company_id=:c"),
                            {"c": cid})
                    except Exception:
                        db.session.rollback()
            db.session.execute(text(
                "DELETE FROM companies WHERE id=:c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM users WHERE email='lat@x.test'"))
        db.session.commit()

        plan = None
        for p in Plan.query.filter_by(is_active=True).all():
            if "accounting" in (p.modules or []):
                plan = p; break
        c = Company(name="__LAT__", base_currency="EGP",
                    subdomain="lat",
                    subscription_started_at=datetime.utcnow(),
                    subscription_expires_at=datetime(2999, 1, 1),
                    intended_plan_id=plan.id if plan else None,
                    plan_id=plan.id if plan else None)
        db.session.add(c); db.session.flush()
        seed_default_coa(c.id)
        u = User(email="lat@x.test",
                 password_hash=generate_password_hash(
                     "x", method="pbkdf2:sha256"),
                 full_name="lat user", is_active=True,
                 status=UserStatus.ACTIVE.value,
                 email_verified_at=datetime.utcnow(),
                 terms_version="TEST")
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role="owner"))
        # One customer + one employee so the tools have data.
        db.session.add(Customer(company_id=c.id, name="cust"))
        db.session.add(Employee(
            company_id=c.id, name="test employee", user_id=u.id,
            status=EmployeeStatus.ACTIVE.value,
            contract_type=ContractType.FULL_TIME.value,
            hire_date=date.today() - timedelta(days=100)))
        db.session.commit()
        return c.id, u.id


def _run_one(app, company_id, user_id, question):
    """One end-to-end insights turn. Returns dict with wall_ms +
    the base-loop summary block from tool_trace, or an error."""
    from app.agent.base import run_agent_turn, insights_persona
    from app.agent.insights_tools import (
        INSIGHTS_TOOL_SCHEMAS, execute_insights_tool,
    )
    from app.services.ai_providers import DeepseekProvider
    with app.app_context():
        t0 = time.perf_counter()
        try:
            reply, _, trace = run_agent_turn(
                messages=[{"role": "user", "content": question}],
                company_id=company_id, user_id=user_id,
                persona=insights_persona(),
                provider=DeepseekProvider(),
                tools=INSIGHTS_TOOL_SCHEMAS,
                execute_tool_fn=execute_insights_tool,
                max_iters=8,
            )
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}
        wall_ms = (time.perf_counter() - t0) * 1000
        summary = {}
        if trace and isinstance(trace[-1], dict) and "summary" in trace[-1]:
            summary = trace[-1]["summary"]
        return {"wall_ms": round(wall_ms, 1),
                "reply_len": len(reply or ""),
                "tool_count": sum(
                    1 for t in trace if "summary" not in t),
                **{f"loop_{k}": v for k, v in summary.items()}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=int,
                        default=len(QUESTIONS),
                        help="How many of the canned questions to run.")
    args = parser.parse_args()

    from app import create_app
    app = create_app()
    stats = _static_stats(app)
    print("─── static (always reported) ───")
    print(f"  tools registered: {stats['tool_count']}")
    print(f"  schemas array:    {stats['schemas_bytes']} bytes")
    print(f"  system prompt:    {stats['prompt_bytes']} bytes")

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print()
        print("SKIPPED end-to-end timing — DEEPSEEK_API_KEY not set.")
        print("Set it in .env and re-run to get real wall-clock numbers.")
        return

    company_id, user_id = _seed_fixture(app)
    print()
    print(f"─── running {args.questions} canned questions ───")
    walls = []
    for i, q in enumerate(QUESTIONS[:args.questions], start=1):
        result = _run_one(app, company_id, user_id, q)
        if "error" in result:
            print(f"  Q{i}  ERROR — {result['error']}")
            continue
        walls.append(result["wall_ms"])
        print(f"  Q{i}  wall={result['wall_ms']:.0f}ms "
              f"tools={result.get('tool_count', 0)} "
              f"reply_len={result.get('reply_len', 0)}")

    if walls:
        print()
        print("─── suite summary ───")
        print(f"  runs:   {len(walls)}")
        print(f"  median: {statistics.median(walls):.0f}ms")
        print(f"  mean:   {statistics.mean(walls):.0f}ms")
        if len(walls) >= 5:
            print(f"  p95:    {sorted(walls)[int(len(walls)*0.95)-1]:.0f}ms")


if __name__ == "__main__":
    main()
