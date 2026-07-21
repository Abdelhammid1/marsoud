"""Playwright end-to-end for MARSOUD-PACK-PRICING (Abdelhamid 2026-07-19).

Drives /products/new in a real browser to prove:

  1. Typing pack_purchase_price + pieces_per_pack live-updates the
     تكلفة الوحدة field to (price / pieces) — no reload, no submit.
  2. When the user then types their own value into unit_cost, the
     auto-derivation stops overwriting it (manual entry wins).
  3. Opening balance shown "→ N قطعة إجمالي" when the user toggles
     the unit to علبة and types a pack count.
  4. Submitting the form actually creates:
       · ProductVariant.unit_cost = derived per-piece price
       · A ProductUnit(كرتونة, factor=pieces) alongside the base
       · A StockBalance in BASE units when opening balance was in packs

Fixture: fresh __PACK_PW__ tenant, one product group + category,
owner user, default warehouse.
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
FIX = "__PACK_PW__"
OWNER_EMAIL = "pack-pw-owner@t.co"
OWNER_PW = "owner12345"


# ─── Fixture ────────────────────────────────────────────────────────
def _fixture():
    from app import create_app, db
    from app.models import (
        Company, User, ProductGroup, ProductCategory, Warehouse,
    )
    from app.models.user import user_companies
    from app.services.roles_seed import (
        seed_permissions_catalog, seed_system_roles_for_company,
    )
    from app.services.seed_coa import seed_default_coa
    from werkzeug.security import generate_password_hash
    from sqlalchemy import text, inspect

    app = create_app()
    with app.app_context():
        insp = inspect(db.engine)
        seed_permissions_catalog()

        def _wipe(name):
            c = Company.query.filter_by(name=name).first()
            if not c:
                return
            with db.engine.begin() as conn:
                conn.execute(text(
                    "DELETE FROM user_companies WHERE company_id = :c"),
                    {"c": c.id})
                # Transitive delete: stock_balances / stock_movements /
                # stock_lots are scoped through variant_id (no
                # company_id column). SQLite has FKs off in dev so the
                # ON DELETE CASCADE on stock_balances.variant_id
                # doesn't fire — leaving orphans that get re-adopted
                # when SQLite reuses the variant PK on the next run.
                for tbl_name in ("stock_balances", "stock_movements",
                                 "stock_lots"):
                    conn.execute(text(
                        f"DELETE FROM {tbl_name} WHERE variant_id IN "
                        "(SELECT id FROM product_variants "
                        " WHERE company_id = :c)"),
                        {"c": c.id})
                for tbl_name in ("payments", "invoice_reminders_sent",
                                 "invoice_items"):
                    conn.execute(text(
                        f"DELETE FROM {tbl_name} WHERE invoice_id IN "
                        "(SELECT id FROM invoices WHERE company_id = :c)"),
                        {"c": c.id})
                for tbl in reversed(db.metadata.sorted_tables):
                    cols = {col["name"] for col in insp.get_columns(tbl.name)}
                    if "company_id" in cols:
                        conn.execute(
                            text(f"DELETE FROM {tbl.name} WHERE company_id = :c"),
                            {"c": c.id})
                conn.execute(text("DELETE FROM companies WHERE id = :c"),
                             {"c": c.id})
                # Zombie sweep for orphans left by older buggy runs.
                for tbl_name in ("stock_balances", "stock_movements",
                                 "stock_lots"):
                    conn.execute(text(
                        f"DELETE FROM {tbl_name} WHERE variant_id NOT IN "
                        "(SELECT id FROM product_variants)"))
                conn.execute(text(
                    "DELETE FROM stock_balances WHERE warehouse_id "
                    "NOT IN (SELECT id FROM warehouses)"))
        _wipe(FIX)
        with db.engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM users WHERE email = :e"),
                {"e": OWNER_EMAIL})
            # Always-run zombie sweep for orphan stock rows left by
            # older buggy runs — runs even when _wipe finds nothing.
            for tbl_name in ("stock_balances", "stock_movements",
                             "stock_lots"):
                conn.execute(text(
                    f"DELETE FROM {tbl_name} WHERE variant_id NOT IN "
                    "(SELECT id FROM product_variants)"))
            conn.execute(text(
                "DELETE FROM stock_balances WHERE warehouse_id NOT IN "
                "(SELECT id FROM warehouses)"))
            conn.execute(text(
                "DELETE FROM invoice_items WHERE invoice_id NOT IN "
                "(SELECT id FROM invoices)"))

        c = Company(name=FIX, base_currency="EGP",
                    vat_rate=0, stock_strict_mode=True)
        db.session.add(c); db.session.flush()
        seed_default_coa(c.id)
        seed_system_roles_for_company(c.id)

        u = User(email=OWNER_EMAIL,
                 password_hash=generate_password_hash(
                     OWNER_PW, method="pbkdf2:sha256"),
                 full_name="pack-pw-owner")
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role="owner"))

        grp = ProductGroup(company_id=c.id, name="بقالة",
                           is_active=True)
        db.session.add(grp); db.session.flush()
        cat = ProductCategory(company_id=c.id, group_id=grp.id,
                              name="مشروبات", is_active=True)
        db.session.add(cat); db.session.flush()
        wh = Warehouse(company_id=c.id, code="MAIN", name="الرئيسي",
                       is_default=True, is_active=True)
        db.session.add(wh); db.session.flush()
        db.session.commit()

        return {"cid": c.id, "uid": u.id, "cat_id": cat.id,
                "grp_id": grp.id, "wh_id": wh.id}


# ─── Flask lifecycle (same recipe as playwright_pos_url_and_confirm) ─
def _start_flask():
    import urllib.request, urllib.error
    subprocess.run(
        ["bash", "-c", "lsof -ti:5000 | xargs -r kill -9"],
        cwd=str(ROOT), check=False, capture_output=True,
    )
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


def _login(page, email, password):
    page.goto(f"{BASE}/login")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")


# ─── Main ───────────────────────────────────────────────────────────
def main():
    fx = _fixture()
    proc = _start_flask()
    passes, failures = [], []

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
            _login(page, OWNER_EMAIL, OWNER_PW)

            # ── /products/new ────────────────────────────────
            page.goto(f"{BASE}/products/new")
            page.wait_for_load_state("networkidle")
            page.fill('input[name="name"]', "بيبسي كولا")
            # Pick the category by group first (dep. dropdowns).
            page.select_option('#group-select',
                               value=str(fx["grp_id"]))
            page.select_option('#category-select',
                               value=str(fx["cat_id"]))

            # Scenario 1 — type pack fields; unit_cost auto-fills.
            page.fill('#pack-price', '60')
            page.fill('#pack-pieces', '24')
            derived = page.locator('#unit-cost').input_value()
            _record(
                "1. Live JS derivation: pack 60 / 24 → per-piece cost",
                derived == '2.5',
                f"unit-cost input = {derived!r}",
            )
            page.screenshot(
                path=SHOT / "pack_01_derived_from_pack.png",
                full_page=True)

            # Scenario 2 — user overrides; further pack edits DO NOT
            # overwrite their manual value.
            page.fill('#unit-cost', '3.00')      # manual override
            page.fill('#pack-price', '90')       # bumps derivation
            override_kept = page.locator('#unit-cost').input_value()
            _record(
                "2. Manual override sticks — pack changes stop touching "
                "unit-cost after the user types their own value",
                override_kept == '3.00' or override_kept == '3',
                f"unit-cost after re-derive = {override_kept!r}",
            )

            # Reset scenario 3 — fresh page, verify opening-qty hint
            # updates when the user toggles the unit to علبة.
            page.goto(f"{BASE}/products/new")
            page.wait_for_load_state("networkidle")
            page.fill('input[name="name"]', "شاي ليبتون")
            page.select_option('#group-select', value=str(fx["grp_id"]))
            page.select_option('#category-select', value=str(fx["cat_id"]))
            page.fill('#pack-price', '80')
            page.fill('#pack-pieces', '40')
            page.fill('#opening-qty', '5')
            page.select_option('#opening-qty-unit', 'pack')
            hint = page.locator('#opening-qty-hint').text_content()
            _record(
                "3. Opening-qty hint tells the user the derived pieces",
                "200" in hint,     # 5 packs × 40 pieces = 200
                f"hint = {hint!r}",
            )
            page.screenshot(
                path=SHOT / "pack_02_opening_qty_hint.png",
                full_page=True)

            # Scenario 4 — actually submit + verify server-side effects.
            # Post-submit we redirect to /products, then read the DB.
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")
            page.screenshot(
                path=SHOT / "pack_03_after_submit.png",
                full_page=True)

            from app import create_app as _ca
            _app = _ca()
            with _app.app_context():
                from app.models import (
                    Product, ProductVariant, ProductUnit, StockBalance,
                )
                p2 = Product.query.filter_by(
                    company_id=fx["cid"], name="شاي ليبتون").one()
                v = ProductVariant.query.filter_by(product_id=p2.id).one()
                units = ProductUnit.query.filter_by(
                    product_id=p2.id).all()
                names_to_factor = {u.unit_name: float(u.conversion_factor)
                                    for u in units}
                bal = StockBalance.query.filter_by(
                    variant_id=v.id).first()
                bal_qty = float(bal.qty) if bal else 0
                bal_per_unit = (float(bal.value) / float(bal.qty)
                                if bal and bal.qty else 0)

            _record(
                "4a. Variant.unit_cost persists as the derived per-piece "
                "cost (80/40 = 2.00)",
                abs(float(v.unit_cost) - 2.0) < 1e-6,
                f"unit_cost = {float(v.unit_cost)}",
            )
            _record(
                "4b. Both base (قطعة ×1) + pack (كرتونة ×40) ProductUnits "
                "created after form submit",
                names_to_factor.get("قطعة") == 1
                and names_to_factor.get("كرتونة") == 40,
                f"units = {names_to_factor}",
            )
            _record(
                "4c. Opening balance stored in BASE units (5 packs × 40 = "
                "200 pieces) at 2.00/unit — ledger sees base units, not packs",
                abs(bal_qty - 200) < 1e-6
                and abs(bal_per_unit - 2.0) < 1e-6,
                f"stock qty = {bal_qty} @ {bal_per_unit:.2f}",
            )

            # ── Gap 1: edit form pack helper ────────────────
            page.goto(f"{BASE}/products/{p2.id}/edit")
            page.wait_for_load_state("networkidle")
            # Expand the (only) variant's <details> row to reveal
            # its edit form + pack helper.
            page.locator("details summary").first.click()
            # Type pack fields inside the variant's row and assert
            # its unit_cost input live-derives.
            form = page.locator("form.pack-scope").first
            form.locator("[data-pack-price]").fill("100")
            form.locator("[data-pack-pieces]").fill("40")
            derived_edit = form.locator("[data-unit-cost]").input_value()
            _record(
                "5. Gap 1 — edit form: typing pack fields inside a "
                "variant row live-fills [data-unit-cost] to price/pieces",
                derived_edit == "2.5",
                f"edit form unit_cost = {derived_edit!r}",
            )
            page.screenshot(
                path=SHOT / "pack_04_edit_form_helper.png",
                full_page=True)

            # ── Gap 3: units page pack cost ──────────────────
            page.goto(f"{BASE}/products/{p2.id}/units")
            page.wait_for_load_state("networkidle")
            # Scope selectors to the add-unit form — the page also has
            # inline row-edit forms with an input[name="unit_name"],
            # which would otherwise match ambiguously.
            page.fill('#add-unit-form input[name="unit_name"]', "شدة")
            page.fill('#add-factor', '10')
            page.fill('#add-pack-cost', '30')
            # Assert live hint reports "= 3" (per-piece).
            hint = page.locator('#add-pack-cost-hint').text_content()
            _record(
                "6. Gap 3 — units page: live hint reports derived "
                "per-piece cost (30 / 10 = 3)",
                "3" in hint,
                f"hint = {hint!r}",
            )
            page.screenshot(
                path=SHOT / "pack_05_units_page_helper.png",
                full_page=True)
            # Submit + assert variant.unit_cost was overwritten by
            # the derivation (previous value was 2.0 from initial
            # opening balance; new should be 3.0).
            page.click('form#add-unit-form button[type="submit"]')
            page.wait_for_load_state("networkidle")
            _app = _ca()
            with _app.app_context():
                from app.models import ProductVariant, ProductUnit
                v2 = ProductVariant.query.filter_by(
                    product_id=p2.id).one()
                units_after = ProductUnit.query.filter_by(
                    product_id=p2.id).all()
                unit_names = {u.unit_name for u in units_after}
            _record(
                "7. Gap 3 — units page POST: variant.unit_cost updated "
                "from pack_purchase_price / factor (30 / 10 → 3.0)",
                abs(float(v2.unit_cost) - 3.0) < 1e-6,
                f"variant.unit_cost = {float(v2.unit_cost)}",
            )
            _record(
                "8. Gap 3 — units page POST also creates the شدة unit "
                "alongside the existing base + كرتونة",
                "شدة" in unit_names,
                f"units = {unit_names}",
            )

            # ── Gap 4: sale-price hint on non-base row ──────
            # Type into the existing كرتونة sale_price and assert
            # a per-piece hint appears somewhere on the page.
            sale_input = page.locator(
                'tr[data-nonbase][data-factor="40"] input[name="sale_price"]'
            ).first
            sale_input.fill("80")
            # Give the JS time to run.
            page.wait_for_timeout(200)
            page_html = page.content()
            _record(
                "9. Gap 4 — sale-price hint reports per-piece rate "
                "(80 / 40 = 2) under the كرتونة sale_price input",
                # Hint text is "= <b>2</b> / قطعة" — just check '2' + 'قطعة'
                # coexist near the input; wrap in a fine check.
                "= <b>2</b>" in page_html and "/ قطعة" in page_html,
                "sale-price hint injected + updated",
            )
            page.screenshot(
                path=SHOT / "pack_06_sale_hint.png",
                full_page=True)

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
