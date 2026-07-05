"""MARSOUD-TZ-BUG (Abdelhamid 2026-07-04) — real-HTTP Playwright.

Reproduces Abdelhamid's exact test in a real browser:

  1. Seed a company + user, a project, a task.
  2. Log in via /login.
  3. Note wall-clock time in Asia/Riyadh right before posting.
  4. POST a comment on the task via the real /tasks/<id>/comments form.
  5. GET the task detail page.
  6. Scrape the rendered comment timestamp from the DOM.
  7. Assert the rendered time is within ~1 minute of the wall-clock —
     NOT +3h (Abdelhamid's bug), NOT -3h either. Any drift > 60s means
     the TZ contract is broken again.
  8. Screenshot the whole page for evidence.
"""
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SHOT = ROOT / "tests" / "screenshots"
SHOT.mkdir(parents=True, exist_ok=True)

BASE = "http://127.0.0.1:5000"


def _login(page, email, password):
    page.goto(f"{BASE}/login")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def _seed():
    from app import create_app, db
    from app.models import (
        Company, User, UserStatus, Project, ProjectStatus,
        Customer, Task, TaskStatus, TaskPriority,
    )
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    from datetime import date, timedelta as td

    app = create_app()
    with app.app_context():
        # Clean prior.
        u_old = User.query.filter_by(email="tzpw@t.co").first()
        if u_old:
            db.session.execute(user_companies.delete().where(
                user_companies.c.user_id == u_old.id))
            db.session.delete(u_old); db.session.commit()

        from sqlalchemy import text, inspect
        old = Company.query.filter_by(name="__TZBUGPW__").first()
        if old:
            insp = inspect(db.engine)
            with db.engine.begin() as conn:
                for tbl in reversed(db.metadata.sorted_tables):
                    cols = {c["name"] for c in insp.get_columns(tbl.name)}
                    if "company_id" in cols:
                        conn.execute(text(
                            f"DELETE FROM {tbl.name} WHERE company_id={old.id}"
                        ))
                conn.execute(text(f"DELETE FROM companies WHERE id={old.id}"))

        co = Company(name="__TZBUGPW__", base_currency="SAR",
                     timezone="Asia/Riyadh")
        db.session.add(co); db.session.flush()
        seed_default_coa(co.id)

        u = User(email="tzpw@t.co", full_name="TZ PW",
                  status=UserStatus.ACTIVE.value)
        u.set_password("tz123!")
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=co.id, role="owner",
        ))

        cust = Customer(company_id=co.id, name="عميل تز")
        db.session.add(cust); db.session.flush()

        p = Project(
            company_id=co.id, name="مشروع تز", type="خدمة",
            customer_id=cust.id, manager_id=u.id,
            start_date=date.today(),
            end_date=date.today() + td(days=7),
            status=ProjectStatus.PLANNING,
        )
        db.session.add(p); db.session.flush()

        t = Task(
            company_id=co.id, project_id=p.id, title="مهمة تز",
            created_by_id=u.id, assigned_to_id=u.id,
            status=TaskStatus.TODO, priority=TaskPriority.MEDIUM,
        )
        db.session.add(t); db.session.commit()

        return {
            "user_id": u.id, "company_id": co.id,
            "task_id": t.id, "project_id": p.id,
        }


def main():
    fx = _seed()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1400, "height": 900}, locale="ar-EG",
        )
        page = ctx.new_page()

        _login(page, "tzpw@t.co", "tz123!")

        # ── Post a comment via the real form ──────────────────────
        page.goto(f"{BASE}/tasks/{fx['task_id']}")
        page.wait_for_load_state("networkidle")

        # Capture wall-clock in Riyadh right around the submit.
        wall_before = datetime.now(ZoneInfo("Asia/Riyadh")).replace(tzinfo=None)

        page.fill('textarea[name="content"]', "اختبار الوقت")
        page.click('form[action*="/comments"] button[type="submit"]')
        page.wait_for_load_state("networkidle")

        wall_after = datetime.now(ZoneInfo("Asia/Riyadh")).replace(tzinfo=None)

        # ── Screenshot the whole task page for the record ────────
        page.goto(f"{BASE}/tasks/{fx['task_id']}")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=SHOT / "tzpw_task_detail.png", full_page=True)

        # ── Scrape the newest comment's timestamp string ─────────
        # The template renders `{{ c.created_at | company_dt('%Y-%m-%d %H:%M') }}`
        # inside a .font-mono div under the comments section.
        html = page.content()
        # Match the last comment block; the timestamp format is
        # `YYYY-MM-DD HH:MM`.
        stamps = re.findall(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", html)
        assert stamps, "no timestamps rendered on the task page"

        # The task itself has a rendered created_at (%Y-%m-%d only) plus
        # activity + comment timestamps in %H:%M format. Filter to those
        # with hours, and pick the latest one — that's the comment we
        # just posted (activities may fire too, but they're within the
        # same second).
        rendered = max(
            datetime.strptime(s, "%Y-%m-%d %H:%M") for s in stamps
        )

        print(f"  wall_before  (Riyadh): {wall_before:%Y-%m-%d %H:%M:%S}")
        print(f"  wall_after   (Riyadh): {wall_after:%Y-%m-%d %H:%M:%S}")
        print(f"  rendered on task page: {rendered:%Y-%m-%d %H:%M}")

        # Rendered comment must be within 60s of the window we captured.
        # Anything +3h or more means the TZ-BUG is back.
        low = wall_before - timedelta(seconds=60)
        high = wall_after + timedelta(seconds=60)
        assert low <= rendered <= high, (
            f"\nBUG RESURGENT — rendered time {rendered} is outside the "
            f"wall-clock window [{low}, {high}].\n"
            f"Drift: {(rendered - wall_after).total_seconds() / 3600:.2f}h "
            f"(should be ~0h, was +3h before the fix)."
        )

        browser.close()

    print("\n✓ Comment rendered within 1 minute of real wall-clock.")
    print(f"  → tests/screenshots/tzpw_task_detail.png")


if __name__ == "__main__":
    main()
