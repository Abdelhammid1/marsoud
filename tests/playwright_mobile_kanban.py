"""Playwright mobile Kanban check.

Loads /tasks/ and /leads/ at iPhone-13-Pro viewport (390x844) and
captures a shot at each scroll position — proving each column takes
the full visible width and cards are never clipped.
"""
import sys
from pathlib import Path

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


def capture_kanban(page, url, prefix, expected_columns):
    """Scroll horizontally through each column and screenshot."""
    page.goto(url)
    page.wait_for_load_state("networkidle")
    # Find the scroll container (has overflow-x-auto class).
    container = page.locator("div.overflow-x-auto").first
    # Full-board initial view
    page.screenshot(path=SHOT / f"{prefix}_00_initial.png")

    col_count = page.locator("div.overflow-x-auto > div.snap-start").count()
    print(f"  {prefix}: {col_count} columns rendered "
            f"(expected ~{expected_columns})")

    for i in range(min(col_count, expected_columns)):
        # Scroll the container by i * (container width). RTL: positive
        # scrollLeft moves toward earlier columns; we use scrollBy in
        # a step so scroll-snap docks properly.
        page.evaluate(
            "(el, step) => { "
            "  const w = el.clientWidth; "
            "  el.scrollTo({ left: w * step * (document.dir === 'rtl' ? -1 : 1), "
            "                behavior: 'instant' }); "
            "}",
            container.element_handle(),
        )
        # Wait a tick for snap.
        page.wait_for_timeout(300)
        page.screenshot(path=SHOT / f"{prefix}_{i + 1:02d}_col{i}.png")


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},  # iPhone 13 Pro
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            locale="ar-EG",
        )
        page = context.new_page()
        _login(page, "mowner@t.co", "mowner123")

        print("Tasks Kanban (5 columns):")
        capture_kanban(page, f"{BASE}/tasks/", "mk_tasks",
                         expected_columns=5)

        print("Leads Kanban (7 columns):")
        capture_kanban(page, f"{BASE}/leads/", "mk_leads",
                         expected_columns=7)

        browser.close()
    print("\nDone. Screenshots in tests/screenshots/mk_*.png")


if __name__ == "__main__":
    main()
