"""Playwright verification for MARSOUD-POS-URL-OPACITY and
MARSOUD-POS-CONFIRM-BEFORE-PAY (Abdelhamid 2026-07-19).

Drives a real browser against the local Flask app so we can see the
actual URL bar + the confirmation modal, not just source-level asserts.

Acceptance criteria (from the ticket):

  Ticket A — URL opacity:
    · Customer can't infer the total number of POS orders across the
      system from the URL.
    · Order numbering shown in the address bar is company-scoped
      (e.g. POS-0001) or random — NOT the global auto-increment id.
    · No impact on internal DB relations (id column keeps working).

  Ticket B — Confirm-before-pay:
    · An order of 2.00 must be payable at exactly 2.00 — the system
      may not silently demand more.
    · No item may appear on the invoice that the cashier did not
      explicitly add to the cart.
    · Any auto-added fee (tax, service, etc.) must be visible to the
      cashier BEFORE checkout.
    · Invoice total must equal the order total the cashier saw.

Fixture:
  · One dedicated company (__POS_PW__), owner user, TWO POS-visible
    products in a single category, a default cash payment method, a
    default warehouse, and stock seeded for each variant so the sale
    doesn't fail on stock_strict_mode.

Scenarios:
  A1. Add one product priced at 2.00 → open confirm modal → assert the
      modal shows exactly ONE line and total = 2.00 (proves the
      customer's "2.00 became 2.50" complaint is now impossible).
  A2. Confirm the order. Assert the browser URL is
      /pos/orders/POS-0001/receipt — the global id (which could be
      any number depending on the shared DB state) is nowhere in the URL.
  A3. Screenshot the receipt page. Assert the receipt shows exactly
      one line with the 2.00 total.
  A4. Visit the legacy /pos/orders/<int_id>/receipt URL directly.
      Assert it still resolves to the same receipt — proves backward
      compat with printed / bookmarked receipts.

  B1. Fresh register load → add TWO products (one at 2.00, one at 3.50).
      Click "دفع" → assert the modal opens, screenshot it, and verify
      the modal lists BOTH product names + the correct grand total.
  B2. Click cancel. Assert cart is intact + modal is hidden.
  B3. Re-open modal → click confirm. Assert order created and URL is
      /pos/orders/POS-0002/receipt (per-company number bumped by 1).

  Cross-tenant probe:
  C1. Create a second company + user. Log in as the second user.
      Try to hit the first company's /pos/orders/<int_id>/receipt.
      Assert 404 — no leak between tenants.
"""
import sys
import time
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SHOT = ROOT / "tests" / "screenshots"
SHOT.mkdir(parents=True, exist_ok=True)

BASE = "http://127.0.0.1:5000"
FIX = "__POS_PW__"      # main company
FIX_B = "__POS_PW_B__"  # second tenant for the cross-tenant probe
OWNER_EMAIL = "pos-pw-owner@t.co"
OWNER_B_EMAIL = "pos-pw-owner-b@t.co"
OWNER_PW = "owner12345"


# ─── Fixture setup ──────────────────────────────────────────────────
def _fixture():
    """Delete + recreate the two test companies so numbering starts
    at POS-0001 for both. Returns (main_cid, main_owner_id,
    b_cid, b_owner_id, main_variants).
    """
    from app import create_app, db
    from app.models import (
        Company, User, PaymentMethod, ProductGroup, ProductCategory,
        Product, ProductVariant, ProductUnit, Warehouse,
    )
    from app.models.user import user_companies
    from app.services.roles_seed import (
        seed_permissions_catalog, seed_system_roles_for_company,
    )
    from app.services.seed_coa import seed_default_coa
    from app.services.inventory import receive_stock
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text, inspect

    app = create_app()
    with app.app_context():
        insp = inspect(db.engine)
        seed_permissions_catalog()

        # ── Nuke old fixture rows so tests are reproducible. ──────
        # SQLite reuses primary keys for deleted rows. If we drop an
        # invoice but its `invoice_items` rows survive (they have no
        # company_id so a blind company-scoped sweep misses them),
        # the next INSERT into `invoices` can be assigned the same
        # id — and the orphan items get re-adopted. That reproduces
        # exactly the "widget line I didn't add" ghost during a rerun.
        # So: transitive-delete every table that hangs off invoices
        # via invoice_id BEFORE the general company-scoped wipe.
        def _wipe(name):
            c = Company.query.filter_by(name=name).first()
            if not c:
                return
            with db.engine.begin() as conn:
                conn.execute(text(
                    "DELETE FROM user_companies WHERE company_id = :c"),
                    {"c": c.id})
                # Transitive cleanup — every table with `invoice_id`
                # but no `company_id` must be cleared FIRST.
                for tbl_name in ("payments", "invoice_reminders_sent",
                                 "invoice_items"):
                    conn.execute(text(
                        f"DELETE FROM {tbl_name} WHERE invoice_id IN "
                        "(SELECT id FROM invoices WHERE company_id = :c)"),
                        {"c": c.id})
                for tbl in reversed(db.metadata.sorted_tables):
                    cols = {col["name"]
                            for col in insp.get_columns(tbl.name)}
                    if "company_id" in cols:
                        conn.execute(
                            text(f"DELETE FROM {tbl.name} "
                                 "WHERE company_id = :c"),
                            {"c": c.id})
                conn.execute(
                    text("DELETE FROM companies WHERE id = :c"),
                    {"c": c.id})
        _wipe(FIX); _wipe(FIX_B)
        # Zombie sweep — orphan rows left over from an OLDER buggy
        # test run before the transitive-delete above was added.
        # Also wipe the owner users so each run is deterministic.
        with db.engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM users WHERE email IN (:a, :b)"),
                {"a": OWNER_EMAIL, "b": OWNER_B_EMAIL})
            for orphan_tbl in ("payments", "invoice_reminders_sent",
                               "invoice_items"):
                conn.execute(text(
                    f"DELETE FROM {orphan_tbl} WHERE invoice_id NOT "
                    "IN (SELECT id FROM invoices)"))

        def _mk_company(name, base_currency="SAR"):
            c = Company(name=name, base_currency=base_currency,
                        vat_rate=0, stock_strict_mode=True)
            db.session.add(c); db.session.flush()
            seed_default_coa(c.id)
            seed_system_roles_for_company(c.id)
            return c

        main = _mk_company(FIX)
        b_co = _mk_company(FIX_B)

        def _mk_owner(email, cid):
            u = User(email=email,
                     password_hash=generate_password_hash(
                         OWNER_PW, method="pbkdf2:sha256"),
                     full_name=email.split("@")[0])
            db.session.add(u); db.session.flush()
            db.session.execute(user_companies.insert().values(
                user_id=u.id, company_id=cid, role="owner"))
            return u
        owner = _mk_owner(OWNER_EMAIL, main.id)
        owner_b = _mk_owner(OWNER_B_EMAIL, b_co.id)

        # seed_default_coa already creates a Cash + Bank
        # PaymentMethod per company, so no manual insert needed.
        # Warehouses.
        wh_main = Warehouse(company_id=main.id, code="MAIN",
                            name="المخزن الرئيسي",
                            is_default=True, is_active=True)
        wh_b = Warehouse(company_id=b_co.id, code="MAIN",
                         name="المخزن الرئيسي",
                         is_default=True, is_active=True)
        db.session.add_all([wh_main, wh_b])
        db.session.flush()

        # One group → one category → TWO products so the confirm-modal
        # test can add two lines from the register grid.
        grp = ProductGroup(company_id=main.id, name="مواد غذائية",
                           is_active=True)
        db.session.add(grp); db.session.flush()
        cat = ProductCategory(company_id=main.id, group_id=grp.id,
                              name="مشروبات", is_active=True)
        db.session.add(cat); db.session.flush()

        def _mk_product(name, sku, price):
            p = Product(company_id=main.id, name=name,
                        category_id=cat.id, default_price=price,
                        default_tax_rate=0,
                        is_active=True, is_tracked=True)
            db.session.add(p); db.session.flush()
            v = ProductVariant(company_id=main.id, product_id=p.id,
                               sku=sku, name="", unit_cost=0,
                               is_active=True)
            db.session.add(v); db.session.flush()
            u = ProductUnit(company_id=main.id, product_id=p.id,
                            unit_name="قطعة", conversion_factor=1,
                            is_base=True)
            db.session.add(u); db.session.flush()
            return p, v

        p1, v1 = _mk_product("لبن جهينة", "MILK-1L", 2.00)
        p2, v2 = _mk_product("عصير مانجو", "JUICE-MANGO", 3.50)

        db.session.commit()

        # Seed stock so stock_strict_mode doesn't reject the sale.
        for v in (v1, v2):
            receive_stock(variant=v, warehouse=wh_main, qty=100,
                          unit_cost=1.00, actor_id=owner.id)
        db.session.commit()

        return {
            "main_cid": main.id, "owner_id": owner.id,
            "b_cid": b_co.id, "b_owner_id": owner_b.id,
            "v1_id": v1.id, "v2_id": v2.id,
            "p1_name": p1.name, "p2_name": p2.name,
        }


# ─── Flask dev server lifecycle ─────────────────────────────────────
def _start_flask():
    """Return the subprocess. Blocks until /login responds."""
    import urllib.request
    import urllib.error
    subprocess.run(
        ["bash", "-c", "lsof -ti:5000 | xargs -r kill -9"],
        cwd=str(ROOT), check=False, capture_output=True,
    )
    # flask_app.py hardcodes debug=True which forks a reloader child;
    # awkward to signal cleanly from a test harness. Boot the app in-
    # place instead, no reloader, so Popen.terminate() actually stops it.
    proc = subprocess.Popen(
        [str(ROOT / ".venv/bin/python3.9"), "-c",
         "from app import create_app; "
         "create_app().run(host='127.0.0.1', port=5000, "
         "debug=False, use_reloader=False)"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/login", timeout=2) as r:
                if r.status == 200:
                    return proc
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.5)
    proc.kill()
    raise RuntimeError("Flask never became ready on :5000")


# ─── Playwright helpers ─────────────────────────────────────────────
def _login(page, email, password):
    page.goto(f"{BASE}/login")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


def _add_product_by_name(page, name):
    """Click a product tile in the category grid by its visible name.
    The tile carries `.cat-item` — we just find one whose innerText
    includes the product name."""
    page.wait_for_selector(".cat-item", state="attached", timeout=5000)
    page.locator(".cat-item", has_text=name).first.click()


# ─── Test main ──────────────────────────────────────────────────────
def main():
    fx = _fixture()
    proc = _start_flask()
    failures = []
    passes = []

    def _record(label, ok, detail=""):
        (passes if ok else failures).append((label, detail))
        marker = "PASS" if ok else "FAIL"
        print(f"{marker}  {label}"
              + (f"  ⇒ {detail}" if detail else ""))

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport={"width": 1400, "height": 900},
                locale="ar-EG",
            )
            page = ctx.new_page()

            # ── Ticket A scenario ────────────────────────────────
            _login(page, OWNER_EMAIL, OWNER_PW)
            page.goto(f"{BASE}/pos/")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=SHOT / "posx_00_register_empty.png",
                            full_page=True)

            # A1 — add لبن جهينة (2.00), open confirm modal, check total.
            _add_product_by_name(page, fx["p1_name"])
            # Sanity: pay button enabled.
            page.wait_for_function(
                "!document.getElementById('pay-btn').disabled")
            page.click("#pay-btn")
            page.wait_for_selector("#confirm-modal",
                                    state="visible", timeout=3000)
            page.screenshot(
                path=SHOT / "posx_01_confirm_modal_2sar.png",
                full_page=True)
            confirm_total = page.locator("#confirm-total").text_content()
            confirm_count = page.locator("#confirm-count").text_content()
            _record(
                "B (accept-criteria) — modal shows exact grand total "
                "matching the cart",
                confirm_total.strip() == "2.00" and confirm_count.strip() == "1",
                f"count={confirm_count!r} total={confirm_total!r}",
            )

            # A2 — confirm, land on receipt page. URL must contain
            # POS-0001 and NOT any bare numeric id path segment.
            page.click("#confirm-modal .btn-primary")
            page.wait_for_load_state("networkidle")
            url_after = page.url
            _record(
                "A (URL opacity) — URL after order shows per-company "
                "POS-0001, not the global id",
                "/pos/orders/POS-0001/receipt" in url_after,
                f"url={url_after}",
            )
            page.screenshot(
                path=SHOT / "posx_02_receipt_pos_0001.png",
                full_page=True)

            # A3 — receipt body shows EXACTLY one line + 2.00 total.
            # This is the tight check: if the server auto-injected an
            # extra line the count would be 2, the total would differ
            # from the modal, and this would fail.
            from app import create_app as _ca
            _app = _ca()
            with _app.app_context():
                from app.models import Invoice
                inv = Invoice.query.filter_by(
                    company_id=fx["main_cid"], number="POS-0001",
                    source="POS").one()
                receipt_lines = len(inv.items)
                receipt_total = float(inv.total)
                receipt_descs = [it.description for it in inv.items]
                receipt_variant_ids = [it.variant_id for it in inv.items]
            _record(
                "B (accept-criteria) — receipt has EXACTLY the lines "
                "the cashier confirmed (no phantom line, no auto-fee)",
                receipt_lines == 1
                and abs(receipt_total - 2.00) < 0.01
                and receipt_descs == [fx["p1_name"]]
                and receipt_variant_ids == [fx["v1_id"]],
                f"lines={receipt_lines} total={receipt_total} "
                f"descs={receipt_descs}",
            )

            # A4 — legacy numeric-id URL still works.
            from app import create_app, db
            app = create_app()
            with app.app_context():
                from app.models import Invoice
                inv = Invoice.query.filter_by(
                    company_id=fx["main_cid"], number="POS-0001",
                    source="POS").one()
                legacy_id = inv.id
            page.goto(f"{BASE}/pos/orders/{legacy_id}/receipt")
            page.wait_for_load_state("networkidle")
            _record(
                "A (backward compat) — legacy /pos/orders/<id>/receipt "
                "still resolves",
                page.locator("text=POS-0001").count() > 0,
                f"legacy_id={legacy_id}",
            )
            page.screenshot(
                path=SHOT / "posx_03_legacy_id_still_works.png",
                full_page=True)

            # ── Ticket B scenario — two items + cancel + reconfirm ──
            page.goto(f"{BASE}/pos/")
            page.wait_for_load_state("networkidle")
            _add_product_by_name(page, fx["p1_name"])   # 2.00
            _add_product_by_name(page, fx["p2_name"])   # 3.50
            page.click("#pay-btn")
            page.wait_for_selector("#confirm-modal",
                                    state="visible", timeout=3000)
            page.screenshot(
                path=SHOT / "posx_04_confirm_modal_two_items.png",
                full_page=True)

            confirm_html = page.locator(
                "#confirm-items").inner_html()
            confirm_total_2 = page.locator(
                "#confirm-total").text_content().strip()
            both_shown = (fx["p1_name"] in confirm_html
                          and fx["p2_name"] in confirm_html)
            _record(
                "B (accept-criteria) — every cart line is visible in "
                "the confirmation modal (no hidden auto-injected items)",
                both_shown and confirm_total_2 == "5.50",
                f"total={confirm_total_2!r} both_shown={both_shown}",
            )

            # B2 — cancel the modal, verify cart still holds 2 lines.
            page.click("#confirm-modal .btn-secondary")
            page.wait_for_selector("#confirm-modal", state="hidden",
                                    timeout=2000)
            cart_lines = page.locator("#cart-rows .rounded-lg").count()
            _record(
                "B (UX) — cancel returns to cart with lines intact",
                cart_lines == 2,
                f"cart_lines={cart_lines}",
            )

            # B3 — confirm the two-line order → land on POS-0002.
            page.click("#pay-btn")
            page.wait_for_selector("#confirm-modal",
                                    state="visible", timeout=3000)
            page.click("#confirm-modal .btn-primary")
            page.wait_for_load_state("networkidle")
            url2 = page.url
            _record(
                "A (URL opacity) — second order gets POS-0002 (per-"
                "company numbering) not the global id",
                "/pos/orders/POS-0002/receipt" in url2,
                f"url={url2}",
            )
            page.screenshot(
                path=SHOT / "posx_05_receipt_pos_0002.png",
                full_page=True)

            # B4 — receipt of POS-0002 must have EXACTLY the two
            # confirmed lines + total 5.50. This is the killer check
            # against server-side auto-injection.
            _app = _ca()
            with _app.app_context():
                from app.models import Invoice
                inv2 = Invoice.query.filter_by(
                    company_id=fx["main_cid"], number="POS-0002",
                    source="POS").one()
                r2_lines = len(inv2.items)
                r2_total = float(inv2.total)
                r2_descs = sorted(it.description for it in inv2.items)
            _record(
                "B (accept-criteria) — 2-item order persists exactly "
                "2 lines with total 5.50 (no phantom, no auto-fee)",
                r2_lines == 2
                and abs(r2_total - 5.50) < 0.01
                and r2_descs == sorted([fx["p1_name"], fx["p2_name"]]),
                f"lines={r2_lines} total={r2_total} descs={r2_descs}",
            )

            # ── Cross-tenant probe ──────────────────────────────
            ctx2 = browser.new_context(
                viewport={"width": 1400, "height": 900},
                locale="ar-EG",
            )
            page2 = ctx2.new_page()
            _login(page2, OWNER_B_EMAIL, OWNER_PW)
            # Tenant B has no POS orders yet — try to fetch tenant
            # A's legacy id URL. Must 404.
            r = page2.goto(f"{BASE}/pos/orders/{legacy_id}/receipt",
                            wait_until="load")
            _record(
                "A (isolation) — tenant B cannot fetch tenant A's "
                "receipt by legacy id",
                r.status == 404,
                f"status={r.status}",
            )
            page2.screenshot(
                path=SHOT / "posx_06_cross_tenant_404.png",
                full_page=True)

            # Also try POS-0001 as an OTHER-tenant. In tenant B this
            # invoice does not exist → 404.
            r = page2.goto(f"{BASE}/pos/orders/POS-0001/receipt",
                            wait_until="load")
            _record(
                "A (isolation) — tenant B's POS-0001 route returns "
                "404 when they haven't created any orders",
                r.status == 404,
                f"status={r.status}",
            )

            browser.close()
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()

    print()
    print(f"────  {len(passes)} passed, {len(failures)} failed  ────")
    print(f"screenshots in {SHOT}")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
