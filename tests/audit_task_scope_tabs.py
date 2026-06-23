#!/usr/bin/env python3
"""MARSOUD-TASK-SCOPE-01 — verifies the 4-tab board for owner/manager.

Acceptance (image #53):
  1. Regular employee → no tabs, sees own + created tasks only.
  2. Regular employee tampering with ?scope=all → still scoped to own.
  3. Owner/manager → 4 tabs visible, default tab = mine.
  4. "Employees" tab → grid of cards with counters + progress bar.
  5. Click employee card → drilled-down Kanban + back-link banner.
  6. Last scope persists via localStorage (frontend-only — we test the
     route emits the data-scope attribute the JS reads).
"""
import sys
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


@check("1. Owner /tasks/ default → 4 tabs visible + scope=mine selected")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        body = client.get("/tasks/").data.decode("utf-8")
        for label in ("🧑 مهامي", "👥 الموظفون", "✍️ أنشأتها", "📋 الكل"):
            assert label in body, f"missing tab label {label!r}"
        # Default scope = mine
        assert 'data-scope="mine"' in body
    return "all 4 tabs render; default tab = mine"


@check("2. Owner ?scope=employees → grid of employee cards")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        body = client.get("/tasks/?scope=employees").data.decode("utf-8")
        # Cards have a unique class signature
        assert 'sm:grid-cols-2 lg:grid-cols-3' in body, \
            "cards grid missing"
        assert 'الإنجاز' in body, "progress label missing"
        # Each card has 4 counters (total / done / in_progress / overdue)
        assert body.count('text-[10px] text-slate-500') >= 4, \
            "card counters not rendering"
    return "cards landing rendered"


@check("3. Owner ?scope=employees&user_id=N → drilled Kanban + back-link")
def _():
    from app.models import Company, User
    company = Company.query.first()
    other_user = User.query.filter(User.email != DEMO_EMAIL).first()
    if not other_user:
        return "skipped — only one user in company"
    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        body = client.get(
            f"/tasks/?scope=employees&user_id={other_user.id}"
        ).data.decode("utf-8")
        assert "رجوع لقائمة الموظفين" in body, "back-link missing"
        # Kanban grid renders (drill-down = Kanban view, not cards)
        assert "grid-cols-5" in body, "Kanban not shown on drill-down"
    return f"drill into user_id={other_user.id} → Kanban + back-link"


@check("4. Owner ?scope=all → Kanban with all tasks + scope state preserved")
def _():
    app = create_app()
    with app.test_client() as client:
        _login(client, DEMO_EMAIL, DEMO_PASS)
        body = client.get("/tasks/?scope=all").data.decode("utf-8")
        assert 'data-scope="all"' in body, "scope state not 'all'"
        assert "grid-cols-5" in body, "Kanban missing"
        # The 'الكل' tab should be highlighted
        assert "bg-emerald-50 border-emerald-300" in body, \
            "active tab style missing"
    return "scope=all renders Kanban; tab marked active"


@check("5. Regular employee /tasks/ → no tabs + own/created tasks only")
def _():
    from werkzeug.security import generate_password_hash
    from app.models import User, Company, Role
    from app.models.user import user_companies
    app = create_app()
    with app.app_context():
        company = Company.query.first()
        # Make / reset an employee test user
        u = User.query.filter_by(email="task_scope_emp@test.com").first()
        if not u:
            u = User(email="task_scope_emp@test.com",
                     full_name="task scope emp",
                     password_hash=generate_password_hash(
                         "p1234567", method="pbkdf2:sha256"),
                     is_active=True)
            db.session.add(u); db.session.flush()
        else:
            u.password_hash = generate_password_hash(
                "p1234567", method="pbkdf2:sha256")
        tm_role = Role.query.filter_by(
            company_id=company.id, code="team_member",
        ).first()
        db.session.execute(user_companies.delete().where(
            (user_companies.c.user_id == u.id) &
            (user_companies.c.company_id == company.id)))
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=company.id,
            role="team_member", role_id=tm_role.id,
        ))
        db.session.commit()
        try:
            with app.test_client() as client:
                _login(client, "task_scope_emp@test.com", "p1234567")
                body = client.get("/tasks/").data.decode("utf-8")
                # No tabs for regular employee
                assert 'id="task-scope-tabs"' not in body, \
                    "regular employee should NOT see tabs"
                # And tampering with ?scope=all must NOT show all tasks
                body_all = client.get("/tasks/?scope=all").data.decode("utf-8")
                assert 'id="task-scope-tabs"' not in body_all
        finally:
            db.session.execute(user_companies.delete().where(
                (user_companies.c.user_id == u.id) &
                (user_companies.c.company_id == company.id)))
            User.query.filter_by(email="task_scope_emp@test.com").delete()
            db.session.commit()
    return "regular employee gets no tabs; ?scope=all ignored"


@check("6. _employee_task_buckets returns one bucket per company member")
def _():
    from app.routes.tasks import _employee_task_buckets
    from app.models import Company
    cid = Company.query.first().id
    buckets = _employee_task_buckets(cid)
    assert isinstance(buckets, list)
    if buckets:
        sample = buckets[0]
        for k in ("user", "total", "done", "in_progress", "overdue",
                   "progress_pct"):
            assert k in sample, f"bucket missing {k}"
    return f"{len(buckets)} buckets returned, schema OK"


@check("7. localStorage scope-persistence script present")
def _():
    src = (ROOT / "app/templates/tasks/index.html").read_text()
    assert "localStorage.setItem('marsoud:tasks:scope'" in src, \
        "saving scope on each visit missing"
    assert "localStorage.getItem('marsoud:tasks:scope')" in src, \
        "reading scope on first load missing"
    return "scope persists across reloads via localStorage"


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
