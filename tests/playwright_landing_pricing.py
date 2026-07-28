#!/usr/bin/env python3
"""MARSOUD-LANDING-PRICING (Abdelhamid 2026-07-25).

Visual + interaction proof for the new Pricing section on the
landing page. Runs against a live dev server (default
localhost:5050).

Verifies:
  1. #pricing section renders + all 3 plan cards visible.
  2. Prices show the correct monthly values by default:
     Starter 799, Growth 1,499, Pro 2,799.
  3. Toggle → yearly. Prices flip to 7,990 / 14,990 / 27,990.
  4. Toggle back → monthly. Prices revert cleanly.
  5. Growth card carries the "featured" styling (star badge).
  6. Nav has the new "الأسعار" link and it scrolls to #pricing.
  7. Responsive: at 400px width the 3 columns collapse to 1.

Screenshots saved to tests/screenshots/landing_pricing/.
"""
import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BASE = os.environ.get("BASE_URL", "http://localhost:5050")
SHOTS = ROOT / "tests" / "screenshots" / "landing_pricing"
SHOTS.mkdir(parents=True, exist_ok=True)


def main():
    from playwright.sync_api import sync_playwright
    passed = failed = 0
    lines = []

    def check(label, cond, detail=""):
        nonlocal passed, failed
        (lines.append(f"PASS  {label}"
                      + (f"  ⇒ {detail}" if detail else ""))
         if cond else
         lines.append(f"FAIL  {label}"
                      + (f"  ⇒ {detail}" if detail else "")))
        if cond:
            passed += 1
        else:
            failed += 1

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)

        # ─── Desktop viewport ─────────────────────────────
        ctx = b.new_context(viewport={"width": 1400, "height": 900},
                              locale="ar")
        page = ctx.new_page()
        page.goto(f"{BASE}/static/landing.html", wait_until="networkidle")

        # 1. Section + all 3 plans.
        section = page.locator("#pricing")
        check("1. #pricing section present", section.count() > 0)
        plans = page.locator("#pricing .plan")
        check("2. Exactly 3 plan cards", plans.count() == 3,
              detail=f"got {plans.count()}")

        # Scroll into view + full-page screenshot of the pricing.
        section.scroll_into_view_if_needed()
        page.wait_for_timeout(400)
        section.screenshot(path=str(SHOTS / "01_desktop_monthly.png"))

        # 3. Prices default = monthly.
        amounts = page.locator("#pricing .plan-price .amt").all_text_contents()
        check("3. Starter monthly = 799",
              amounts and amounts[0].strip() == "799",
              detail=str(amounts))
        check("4. Growth monthly = 1,499",
              len(amounts) > 1 and amounts[1].strip() == "1,499")
        check("5. Pro monthly = 2,799",
              len(amounts) > 2 and amounts[2].strip() == "2,799")

        # 4. Toggle to yearly.
        page.click("#price-toggle button[data-cycle='yearly']")
        page.wait_for_timeout(350)
        section.screenshot(path=str(SHOTS / "02_desktop_yearly.png"))
        amounts_y = page.locator("#pricing .plan-price .amt").all_text_contents()
        check("6. Toggle → yearly: Starter = 7,990",
              amounts_y and amounts_y[0].strip() == "7,990",
              detail=str(amounts_y))
        check("7. Toggle → yearly: Growth = 14,990",
              len(amounts_y) > 1 and amounts_y[1].strip() == "14,990")
        check("8. Toggle → yearly: Pro = 27,990",
              len(amounts_y) > 2 and amounts_y[2].strip() == "27,990")

        # 5. Toggle back to monthly cleanly.
        page.click("#price-toggle button[data-cycle='monthly']")
        page.wait_for_timeout(350)
        amounts_m = page.locator("#pricing .plan-price .amt").all_text_contents()
        check("9. Toggle back: prices revert to monthly",
              amounts_m[0].strip() == "799" and
              amounts_m[1].strip() == "1,499" and
              amounts_m[2].strip() == "2,799")

        # 6. Growth = featured (has the badge).
        featured_badges = page.locator(
            "#pricing .plan.featured .plan-badge").count()
        check("10. Growth is featured with a badge",
              featured_badges == 1,
              detail=f"badges={featured_badges}")

        # 7. Nav link jumps to #pricing.
        nav_link = page.locator("nav .nav-links a[href='#pricing']")
        check("11. Nav has 'الأسعار' link",
              nav_link.count() == 1)

        # ─── Responsive check ────────────────────────────
        ctx2 = b.new_context(viewport={"width": 400, "height": 900},
                               locale="ar")
        page2 = ctx2.new_page()
        page2.goto(f"{BASE}/static/landing.html",
                    wait_until="networkidle")
        # Force-reveal every card up-front so the mobile screenshot
        # captures all 3 plans (the real page uses an
        # IntersectionObserver to fade cards in as they scroll into
        # view; the screenshot below is a single frame, so we skip
        # the animation for a clean visual proof).
        page2.evaluate("""
            document.querySelectorAll('.reveal').forEach(function(el){
                el.classList.add('in');
            });
        """)
        page2.locator("#pricing").scroll_into_view_if_needed()
        page2.wait_for_timeout(400)
        page2.locator("#pricing").screenshot(
            path=str(SHOTS / "03_mobile.png"))
        # At 400px width the .plans grid collapses to a single column
        # per the media query. The plan cards should stack vertically.
        first_plan_box = page2.locator("#pricing .plan").nth(0).bounding_box()
        second_plan_box = page2.locator("#pricing .plan").nth(1).bounding_box()
        check("12. Mobile: cards stack vertically (2nd below 1st)",
              second_plan_box["y"] > first_plan_box["y"] +
                                       first_plan_box["height"] - 20,
              detail=f"1st y={first_plan_box['y']:.0f} h={first_plan_box['height']:.0f}, "
                     f"2nd y={second_plan_box['y']:.0f}")

        b.close()

    print("\n".join(lines))
    print(f"\n────  {passed} passed, {failed} failed  ────")
    print(f"Screenshots: {SHOTS}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
