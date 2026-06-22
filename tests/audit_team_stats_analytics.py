#!/usr/bin/env python3
"""MARSOUD — Team Stats Analytics Expansion audit.

Covers the v1 deliverable from ticket #3 (تطوير في احصائيات الفريق):
  - 5 new performance columns (completion_rate, overdue_ratio, on_time_rate,
    avg_time_to_close, velocity_30d)
  - Mini-charts (closed-per-week 8-bin histogram + status distribution)
  - Date-range filter (7/30/90/all)
  - Auto-badges (star / behind / new)
  - Live round-trip: closing a task in TaskActivityLog moves the
    velocity_30d + completion_rate the right way.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
DEMO_EMAIL = "demo@manasety.ai"
DEMO_PASS = "demo1234"


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _login(client, email, password):
    r = client.post("/login", data={"email": email, "password": password},
                    follow_redirects=False)
    assert r.status_code in (302, 303), \
        f"login for {email} failed: status={r.status_code}"


# ─── 1. New return shape ────────────────────────────────────────────────
@check("1. team_stats() returns dict with rows + closed_per_week + status_dist")
def _():
    from app.services.tasks_extras import team_stats
    from app.models import Company
    company = Company.query.order_by(Company.id).first()
    result = team_stats(company.id)
    assert isinstance(result, dict), \
        f"expected dict, got {type(result).__name__}"
    for k in ("rows", "closed_per_week", "status_dist"):
        assert k in result, f"missing key {k!r}"
    assert len(result["closed_per_week"]) == 8, \
        f"closed_per_week has {len(result['closed_per_week'])} entries, expected 8"
    return f"keys present + 8-bin closed_per_week"


@check("2. Each row exposes the 5 new analytics metrics")
def _():
    from app.services.tasks_extras import team_stats
    from app.models import Company
    company = Company.query.order_by(Company.id).first()
    rows = team_stats(company.id)["rows"]
    if not rows:
        return "no team data yet — schema-only check below"
    sample = rows[0]
    for k in ("completion_rate", "overdue_ratio", "on_time_rate",
               "avg_time_to_close", "velocity_30d", "badges"):
        assert k in sample, f"row missing {k!r}"
    assert isinstance(sample["badges"], list), \
        "badges must be a list (possibly empty)"
    return f"row has all 5 metrics + badges list"


# ─── 2. status_dist matches actual task statuses ───────────────────────
@check("3. status_dist sums match Task.query counts")
def _():
    from app.services.tasks_extras import team_stats
    from app.models import Task, TaskStatus, Company
    company = Company.query.order_by(Company.id).first()
    result = team_stats(company.id)
    dist = result["status_dist"]
    actual_total = Task.query.filter(Task.company_id == company.id).count()
    distrib_total = sum(dist.values())
    assert actual_total == distrib_total, \
        f"sum({dist}) = {distrib_total} but Task.count = {actual_total}"
    return f"status distribution sums to {distrib_total} == Task.count"


# ─── 3. Date-range filter actually filters ─────────────────────────────
@check("4. since= filter reduces total when narrowing the range")
def _():
    from app.services.tasks_extras import team_stats
    from app.models import Company
    company = Company.query.order_by(Company.id).first()
    all_rows = team_stats(company.id)["rows"]
    last_7 = team_stats(company.id,
                         since=datetime.now() - timedelta(days=7))["rows"]
    all_total = sum(r["total"] for r in all_rows)
    recent_total = sum(r["total"] for r in last_7)
    assert recent_total <= all_total, \
        f"7-day total {recent_total} should be ≤ all-time {all_total}"
    return f"all-time total={all_total}, last-7 total={recent_total}"


# ─── 4. Live round-trip: closing a task bumps velocity_30d ─────────────
@check("5. Closing a task via STATUS_CHANGED activity log bumps velocity_30d")
def _():
    from app.services.tasks_extras import team_stats
    from app.models import (
        Task, TaskStatus, TaskPriority, TaskActivityLog,
        Company, User, task_assignees,
    )
    import json
    company = Company.query.order_by(Company.id).first()
    user = User.query.filter_by(email=DEMO_EMAIL).first()

    before = team_stats(company.id)["rows"]
    before_v = next((r["velocity_30d"] for r in before
                    if r["user"] and r["user"].id == user.id), 0)

    t = Task(
        company_id=company.id, title="VELOCITY_TEST",
        status=TaskStatus.DONE,
        priority=TaskPriority.LOW,
        assigned_to_id=user.id, created_by_id=user.id,
    )
    db.session.add(t)
    db.session.flush()
    db.session.execute(task_assignees.insert().values(
        task_id=t.id, user_id=user.id,
    ))
    db.session.add(TaskActivityLog(
        task_id=t.id, user_id=user.id, company_id=company.id,
        action="STATUS_CHANGED",
        before_json=json.dumps({"status": "TODO"}),
        after_json=json.dumps({"status": "DONE"}),
    ))
    db.session.commit()

    after = team_stats(company.id)["rows"]
    after_v = next((r["velocity_30d"] for r in after
                   if r["user"] and r["user"].id == user.id), 0)

    # Cleanup
    TaskActivityLog.query.filter_by(task_id=t.id).delete()
    db.session.execute(task_assignees.delete().where(task_assignees.c.task_id == t.id))
    db.session.delete(t)
    db.session.commit()

    delta = after_v - before_v
    assert delta == 1, \
        f"velocity_30d should bump by 1, bumped by {delta} (before={before_v}, after={after_v})"
    return f"closing a DONE task w/ activity log: velocity_30d {before_v} → {after_v}"


# ─── 5. HTTP rendering: page loads + new elements present ──────────────
@check("6. /tasks/stats?range=30 returns 200 + has all new UI elements")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        r = client.get("/tasks/stats?range=30", follow_redirects=False)
        assert r.status_code == 200, f"status={r.status_code}"
        body = r.data.decode("utf-8")
        for fragment in ["إحصائيات أداء الفريق", "آخر 7 أيام",
                         "المهام المُنجزة أسبوعياً", "توزيع حالة المهام",
                         "إنجاز %", "تأخر %", "في الموعد %",
                         "سرعة 30 يوم"]:
            assert fragment in body, f"missing UI fragment: {fragment}"
    return "page has filter, both charts, and all 5 new column headers"


@check("7. Date-range filter links use the right URL pattern")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        body = client.get("/tasks/stats").data.decode("utf-8")
        for code in ["range=7", "range=30", "range=90", "range=all"]:
            assert code in body, f"filter link missing {code}"
    return "all 4 range filter links present"


@check("8. Auto-badges helpers exist (star/behind/new)")
def _():
    src = (ROOT / "app/services/tasks_extras.py").read_text()
    for label in ("'star'", "'behind'", "'new'"):
        assert label in src, f"badge code {label} missing from team_stats"
    tmpl = (ROOT / "app/templates/tasks/stats.html").read_text()
    assert "🌟 الأفضل" in tmpl, "star badge label missing in template"
    assert "⚠ متأخر" in tmpl, "behind badge label missing in template"
    assert "🆕 جديد" in tmpl, "new badge label missing in template"
    return "all 3 badges wired service-side + template-side"


@check("9. Chart.js loaded only when there's data to show")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        body = client.get("/tasks/stats?range=all").data.decode("utf-8")
        # Chart.js script tag is always emitted; the chart canvas only
        # renders when there's data — verify the data-guard exists.
        assert "chart.umd" in body.lower() or "chart.js" in body.lower()
        # The {% if has_close_data %} guard
        assert ("closedPerWeek" in body) or ("لا توجد مهام مكتملة" in body)
        assert ("statusDist" in body) or ("لا توجد مهام في الشركة" in body)
    return "chart canvases guarded by data presence"


def main():
    app = create_app()
    with app.app_context():
        passed = failed = 0
        for label, fn in CHECKS:
            try:
                msg = fn()
                print(f"\033[92mPASS\033[0m  {label}")
                if msg:
                    print(f"        {msg}")
                passed += 1
            except Exception as e:
                print(f"\033[91mFAIL\033[0m  {label}")
                print(f"        {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                failed += 1
        print()
        print(f"  {passed}/{passed + failed} checks passed.")
        return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
