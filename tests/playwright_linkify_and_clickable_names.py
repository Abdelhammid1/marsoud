#!/usr/bin/env python3
"""MARSOUD-LINKIFY + MARSOUD-CLICKABLE-ASSIGNEE — end-to-end verify.

Seeds a task with a URL in the description + a comment carrying
both a URL and an @-mention, then opens the task in Chromium
against a live Flask server at http://127.0.0.1:5050.
"""
import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5050")
SHOTS = ROOT / "tests" / "screenshots" / "linkify_and_clickable_names"
SHOTS.mkdir(parents=True, exist_ok=True)


def _spin_up():
    from app import create_app, db
    from app.models import (
        Company, User, Task, TaskStatus, TaskPriority, TaskComment,
    )
    app = create_app()
    with app.app_context():
        company = Company.query.first()
        owner = User.query.filter_by(email='demo@manasety.ai').first()
        # Wipe prior fixtures.
        Task.query.filter(Task.title.like('PW-LCA-%')).delete()
        db.session.commit()
        t = Task(
            company_id=company.id,
            title="PW-LCA-Task",
            description="see https://marsoud.com/help for the full flow",
            assigned_to_id=owner.id, created_by_id=owner.id,
            priority=TaskPriority.MEDIUM, status=TaskStatus.TODO,
        )
        db.session.add(t); db.session.flush()
        db.session.add(TaskComment(
            task_id=t.id, user_id=owner.id, company_id=company.id,
            content=(f"hey @[demo]({{'user'}}:{owner.id}) reports at "
                     "https://marsoud.com/reports/metric-logs -"
                     " تقرير قيود المتريك للموظفين"),
        ))
        db.session.commit()
        return {'task_id': t.id, 'owner_id': owner.id}


def _cleanup():
    from app import create_app, db
    from app.models import Task
    app = create_app()
    with app.app_context():
        Task.query.filter(Task.title.like('PW-LCA-%')).delete()
        db.session.commit()


def main():
    from playwright.sync_api import sync_playwright
    seed = _spin_up()
    passed = failed = 0
    fails = []

    def _record(ok, label, details=""):
        nonlocal passed, failed
        if ok:
            print(f"PASS  {label}")
            passed += 1
        else:
            print(f"FAIL  {label}  ⇒ {details}")
            failed += 1
            fails.append(f"{label}: {details}")

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            ctx = b.new_context(
                viewport={"width": 1600, "height": 1000}, locale="ar",
            )
            page = ctx.new_page()

            page.goto(f"{BASE}/login", wait_until="domcontentloaded")
            page.fill('input[name="email"]', "demo@manasety.ai")
            page.fill('input[name="password"]', "demo1234")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")

            page.goto(f"{BASE}/tasks/{seed['task_id']}",
                       wait_until="networkidle")
            page.screenshot(path=str(SHOTS / "01_task_detail.png"),
                            full_page=True)
            html = page.content()

            # Check 1: URL in description is an <a href=...>
            _record(
                'href="https://marsoud.com/help"' in html,
                "1. URL in description rendered as clickable anchor",
                "description URL not linkified",
            )

            # Check 2: URL in comment is also an <a href=...>
            _record(
                'href="https://marsoud.com/reports/metric-logs"' in html,
                "2. URL in comment rendered as clickable anchor",
                "comment URL not linkified",
            )

            # Check 3: creator badge in header links to their tasks board
            _record(
                f'user_id={seed["owner_id"]}' in html
                and 'بواسطة' in html,
                "3. creator name in header is clickable",
                "creator link missing",
            )

            # Check 4: URL anchor has target=_blank + noopener
            _record(
                'target="_blank"' in html
                and 'rel="noopener noreferrer"' in html,
                "4. anchors carry target=_blank + noopener",
                "safety attrs missing",
            )
            b.close()
    finally:
        _cleanup()
        print()
        print(f"────  {passed} passed, {failed} failed  ────")
        if fails:
            for line in fails:
                print(f"  · {line}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
