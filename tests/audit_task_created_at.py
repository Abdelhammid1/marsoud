#!/usr/bin/env python3
"""MARSOUD-TASK-CREATED-AT (Abdelhamid 2026-07-22).

Task cards didn't show creation date; no sort / filter by created_at.
Now: relative_date filter, ?sort=newest|oldest, and ?created_range=today|
yesterday|last7|last30|this_month|last_month|custom (with from/to).

Checks:
  1. relative_date filter humanizes now → "اليوم", yesterday → "أمس",
     3 days ago → "قبل 3 أيام", 10 days ago → absolute date.
  2. /tasks/?sort=newest returns tasks in DESC created_at order.
  3. /tasks/?sort=oldest returns tasks in ASC created_at order.
  4. /tasks/?created_range=today returns only tasks created today.
  5. /tasks/?created_range=yesterday returns only tasks from yesterday.
  6. /tasks/?created_range=custom&from=X&to=Y filters by that range,
     inclusive of both ends.
  7. Task index page renders the "📅 قبل N أيام" line + quick-pill
     row + sort selector (source check).
"""
import os
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

os.environ["MARSOUD_ORPHAN_SWEEP_ON_BOOT"] = "0"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app, db


CHECKS = []
_STATE = {}


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _wipe(name):
    from app.models import Company
    from sqlalchemy import text, inspect
    c = Company.query.filter_by(name=name).first()
    if not c:
        return
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM user_companies WHERE company_id = :c"),
                     {"c": c.id})
        for tbl in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(tbl.name)}
            if "company_id" in cols:
                conn.execute(
                    text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                    {"c": c.id})
        conn.execute(text("DELETE FROM companies WHERE id = :c"),
                     {"c": c.id})


def _setup():
    from app.models import (
        Company, User, Task, TaskStatus, TaskPriority,
    )
    from app.models.user import user_companies
    from app.services.roles_seed import (
        seed_permissions_catalog, seed_system_roles_for_company,
    )
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text

    seed_permissions_catalog()
    _wipe("__TASK_CREATED__")
    with db.engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM users WHERE email = 'tc-owner@x.test'"))

    c = Company(name="__TASK_CREATED__", base_currency="EGP")
    db.session.add(c); db.session.flush()
    seed_default_coa(c.id)
    seed_system_roles_for_company(c.id)
    # MARSOUD-CHOOSE-PLAN — pretend the owner already picked a plan
    # so the choose-plan middleware doesn't hijack /tasks/ requests.
    # Prefer enterprise (unrestricted subitems) so plan_gating
    # doesn't block /tasks/ either — this audit isn't about plans.
    from app.models import Plan
    p = Plan.query.filter_by(code="enterprise").first() \
        or Plan.query.filter_by(is_active=True).first()
    if p:
        c.plan_id = p.id
        c.intended_plan_id = p.id
    # A future trial window so during-trial gating gets bypassed too.
    from datetime import datetime as _dt, timedelta as _td
    c.subscription_expires_at = _dt.utcnow() + _td(days=30)
    db.session.flush()

    u = User(email="tc-owner@x.test",
             password_hash=generate_password_hash("x", method="pbkdf2:sha256"),
             full_name="tc-owner")
    db.session.add(u); db.session.flush()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role="owner"))
    db.session.flush()

    # 4 tasks: today, yesterday, 3 days ago, 10 days ago.
    now = datetime.utcnow()
    fixtures = [
        ("T_TODAY",    now - timedelta(hours=2)),
        ("T_YEST",     now - timedelta(days=1, hours=3)),
        ("T_3DAYS",    now - timedelta(days=3)),
        ("T_10DAYS",   now - timedelta(days=10)),
    ]
    ids = {}
    for title, created in fixtures:
        t = Task(company_id=c.id, title=title, status=TaskStatus.TODO,
                 priority=TaskPriority.MEDIUM, assigned_to_id=u.id,
                 created_by_id=u.id, created_at=created,
                 updated_at=created)
        db.session.add(t); db.session.flush()
        # created_at is set by default=datetime.utcnow at insert; force it.
        db.session.execute(
            text("UPDATE tasks SET created_at = :ca WHERE id = :i"),
            {"ca": created, "i": t.id})
        ids[title] = t.id
    db.session.commit()

    _STATE.update(cid=c.id, uid=u.id, ids=ids)


def _login(cid, uid):
    from flask import current_app, g
    for k in ("_login_user", "active_company"):
        try: g.pop(k, None)
        except Exception: pass
    client = current_app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
        sess["_fresh"] = True
        sess["active_company_id"] = cid
    return client


@check("1. relative_date filter — today/yesterday/N days/absolute")
def _():
    from flask import current_app
    now = datetime.utcnow()
    filt = current_app.jinja_env.filters["relative_date"]
    assert filt(None) == "—"
    assert filt(now) == "اليوم"
    assert filt(now - timedelta(days=1)) == "أمس"
    assert filt(now - timedelta(days=3)) == "قبل 3 أيام"
    older = now - timedelta(days=10)
    out = filt(older)
    # Absolute format for older values — must contain the year.
    assert str(now.year) in out or str((now - timedelta(days=10)).year) in out, \
        f"older date should have year, got {out!r}"
    return "همفدلر ok"


@check("2. ?sort=newest returns DESC by created_at")
def _():
    c = _login(_STATE["cid"], _STATE["uid"])
    r = c.get("/tasks/?scope=all&sort=newest")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # Find the order of task titles in the rendered HTML.
    order = []
    for name in ("T_TODAY", "T_YEST", "T_3DAYS", "T_10DAYS"):
        idx = html.find(name)
        if idx != -1:
            order.append((idx, name))
    order.sort()
    got = [n for _, n in order]
    assert got == ["T_TODAY", "T_YEST", "T_3DAYS", "T_10DAYS"], \
        f"got order {got}"
    return "newest → today, yesterday, 3d, 10d"


@check("3. ?sort=oldest returns ASC by created_at")
def _():
    c = _login(_STATE["cid"], _STATE["uid"])
    r = c.get("/tasks/?scope=all&sort=oldest")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    order = []
    for name in ("T_TODAY", "T_YEST", "T_3DAYS", "T_10DAYS"):
        idx = html.find(name)
        if idx != -1:
            order.append((idx, name))
    order.sort()
    got = [n for _, n in order]
    assert got == ["T_10DAYS", "T_3DAYS", "T_YEST", "T_TODAY"], \
        f"got order {got}"
    return "oldest → 10d, 3d, yesterday, today"


@check("4. ?created_range=today → only today's tasks")
def _():
    c = _login(_STATE["cid"], _STATE["uid"])
    r = c.get("/tasks/?scope=all&created_range=today")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "T_TODAY" in html
    for n in ("T_YEST", "T_3DAYS", "T_10DAYS"):
        assert n not in html, f"{n} shouldn't be here"
    return "T_TODAY only"


@check("5. ?created_range=yesterday → only yesterday's task")
def _():
    c = _login(_STATE["cid"], _STATE["uid"])
    r = c.get("/tasks/?scope=all&created_range=yesterday")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "T_YEST" in html
    for n in ("T_TODAY", "T_3DAYS", "T_10DAYS"):
        assert n not in html, f"{n} shouldn't be here"
    return "T_YEST only"


@check("6. ?created_range=custom&from=X&to=Y filters inclusively")
def _():
    c = _login(_STATE["cid"], _STATE["uid"])
    # Range covering 3 days ago through today (should include T_TODAY,
    # T_YEST, T_3DAYS but exclude T_10DAYS).
    d_to = date.today().isoformat()
    d_from = (date.today() - timedelta(days=3)).isoformat()
    r = c.get(f"/tasks/?scope=all&created_range=custom"
              f"&from={d_from}&to={d_to}")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for n in ("T_TODAY", "T_YEST", "T_3DAYS"):
        assert n in html, f"{n} missing from custom range"
    assert "T_10DAYS" not in html
    return "custom range inclusive of both ends"


@check("7. Card renders 📅 line + quick-pill row + sort selector")
def _():
    c = _login(_STATE["cid"], _STATE["uid"])
    r = c.get("/tasks/?scope=all")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "📅" in html, "created-at line missing from card"
    assert "الأحدث أولاً" in html, "sort selector missing"
    assert "آخر 7 أيام" in html, "quick-pill row missing"
    return "UI elements present"


def main():
    app = create_app()
    passed = failed = 0
    with app.app_context():
        try:
            _setup()
            for label, fn in CHECKS:
                try:
                    res = fn()
                    print(f"PASS  {label}  ⇒ {res}")
                    passed += 1
                except Exception as e:  # noqa: BLE001
                    print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
                    failed += 1
                    import traceback; traceback.print_exc()
        finally:
            _wipe("__TASK_CREATED__")
            print("\n(cleaned up)")
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
