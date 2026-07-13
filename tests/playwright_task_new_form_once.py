#!/usr/bin/env python3
"""MARSOUD-FORM-ONCE (Abdelhamid 2026-07-13) — verify in a real
browser that the new-task form's submit button:

  · disables after the first click, so a fast double-click can't
    create two tasks, and
  · swaps its label to 'جاري الإنشاء...' (the create-specific
    variant of the comment button's 'جاري الإرسال...').

Server assumed running on http://127.0.0.1:5050.
"""
import os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5050")
SHOTS = ROOT / "tests" / "screenshots" / "task_new_form_once"
SHOTS.mkdir(parents=True, exist_ok=True)


def _cleanup():
    from app import create_app, db
    from app.models import Task
    app = create_app()
    with app.app_context():
        Task.query.filter(Task.title.like("PW-ONCE-%")).delete()
        db.session.commit()


def main():
    from playwright.sync_api import sync_playwright
    _cleanup()
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

            # Login as the seeded demo owner.
            page.goto(f"{BASE}/login", wait_until="domcontentloaded")
            page.fill('input[name="email"]', "demo@manasety.ai")
            page.fill('input[name="password"]', "demo1234")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")

            # Open the new-task form.
            page.goto(f"{BASE}/tasks/new", wait_until="networkidle")

            # ── Check 1 — form is annotated correctly ─────────────
            form_html = page.locator("form[data-once]").first
            has_form = form_html.count() > 0
            _record(
                has_form,
                "1. new-task form carries data-once",
                "no form[data-once] on the page",
            )
            once_label = form_html.get_attribute("data-once-label") if has_form else ""
            _record(
                (once_label or "").strip() == "جاري الإنشاء...",
                "2. form declares data-once-label='جاري الإنشاء...'",
                f"got {once_label!r}",
            )

            # Fill in a valid task so the submit doesn't 400 out.
            page.fill('input[name="title"]', "PW-ONCE-Test")
            first_user = page.eval_on_selector(
                'input[name="assignee_ids"]',
                'e => e.value',
            )
            if not first_user:
                # Multi-checkbox layout — pick the first checkbox.
                page.check('input[type="checkbox"][name="assignee_ids"]')

            # ── Checks 3+4 — synthesize a submit event WITHOUT
            # actually navigating, then read the DOM state that the
            # MARSOUD-FORM-ONCE guard installed. Cancelling the
            # default in a post-guard listener keeps the browser
            # on this page so we can inspect the button. This
            # avoids the race that Playwright's page.route() has
            # against navigation-triggered submits. #####
            captured = page.evaluate("""() => {
                const form = document.querySelector('form[data-once]');
                if (!form) return {error: 'no form[data-once] found'};
                const btn = form.querySelector(
                    'button[type="submit"], input[type="submit"]');
                if (!btn) return {error: 'no submit button in form'};
                // Cancel the actual navigation AFTER the guard fires
                // (guard is on capture; our listener is on bubble).
                form.addEventListener('submit', ev => ev.preventDefault(),
                                        {once: true});
                form.requestSubmit
                    ? form.requestSubmit()
                    : form.dispatchEvent(new Event('submit',
                                                    {cancelable: true,
                                                     bubbles: true}));
                return {
                    label: (btn.textContent || btn.value || '').trim(),
                    disabled: btn.disabled === true,
                };
            }""")
            _record(
                captured.get("label") == "جاري الإنشاء...",
                "3. button label swaps to 'جاري الإنشاء...' during submit",
                f"got label {captured.get('label')!r}",
            )
            _record(
                captured.get("disabled") is True,
                "4. button becomes disabled during submit",
                f"got disabled={captured.get('disabled')!r}",
            )

            # Now do a REAL submit so we can also verify no duplicate
            # is created at the DB layer (the whole point of the guard).
            page.reload(wait_until="networkidle")
            page.fill('input[name="title"]', "PW-ONCE-Test")
            page.check('input[type="checkbox"][name="assignee_ids"]')
            page.locator(
                'button[type="submit"]:has-text("إنشاء")'
            ).first.click()
            page.wait_for_load_state("networkidle")

            # ── Check 5 — only ONE task got created (no dupe) ─────
            from app import create_app, db
            from app.models import Task
            app = create_app()
            with app.app_context():
                count = Task.query.filter_by(
                    title="PW-ONCE-Test").count()
            _record(
                count == 1,
                "5. exactly ONE task was created (no duplicate)",
                f"got {count} tasks with the test title",
            )

            b.close()
    finally:
        _cleanup()
        print()
        print(f"────  {passed} passed, {failed} failed  ────")
        if fails:
            for f in fails:
                print(f"  · {f}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
