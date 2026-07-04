"""MARSOUD-MC-EMPLOYEE (Abdelhamid 2026-07-04) — real-HTTP Playwright.

Real-browser proof that Abdelhamid's bug is fixed:

  1. Seed one User + three Companies. Give the User an Employee row in
     each (via Employee.user_id) and the "owner" role in each company.
  2. Log in via /login.
  3. For each of the three companies:
       - Switch active_company via /auth/switch-company/<id>
       - GET /my/account → asserts we DON'T see the
         "هذه الصفحة للموظفين المرتبطين بسجل HR فقط" flash
         (this was the exact page Abdelhamid screenshotted).
       - Screenshot the account page for the record.
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


def _seed_fixture():
    from app import create_app, db
    from app.models import Company, User, UserStatus, Employee, EmployeeStatus
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

    app = create_app()
    with app.app_context():
        # Clean prior fixture.
        u_old = User.query.filter_by(email="mce_pw_owner@t.co").first()
        if u_old:
            db.session.execute(user_companies.delete().where(
                user_companies.c.user_id == u_old.id))
            db.session.delete(u_old); db.session.commit()

        from sqlalchemy import text, inspect
        for name in ("__MCEPW_A__", "__MCEPW_B__", "__MCEPW_C__"):
            old = Company.query.filter_by(name=name).first()
            if old:
                insp = inspect(db.engine)
                with db.engine.begin() as conn:
                    for tbl in reversed(db.metadata.sorted_tables):
                        cols = {c["name"] for c in insp.get_columns(tbl.name)}
                        if "company_id" in cols:
                            conn.execute(text(
                                f"DELETE FROM {tbl.name} WHERE company_id = :c"
                            ), {"c": old.id})
                    conn.execute(text("DELETE FROM companies WHERE id = :c"),
                                   {"c": old.id})

        companies = []
        for name, cur in (("__MCEPW_A__", "SAR"),
                          ("__MCEPW_B__", "EGP"),
                          ("__MCEPW_C__", "EGP")):
            co = Company(name=name, base_currency=cur)
            db.session.add(co)
            companies.append(co)
        db.session.flush()
        for co in companies:
            seed_default_coa(co.id)

        u = User(
            email="mce_pw_owner@t.co", full_name="MC-EMP PW Owner",
            status=UserStatus.ACTIVE.value,
        )
        u.set_password("mce123!")
        db.session.add(u); db.session.flush()

        for co in companies:
            db.session.execute(user_companies.insert().values(
                user_id=u.id, company_id=co.id, role="owner",
            ))
            e = Employee(
                company_id=co.id, name=u.full_name, email=u.email,
                user_id=u.id, status=EmployeeStatus.ACTIVE,
            )
            db.session.add(e)
        db.session.commit()

        return {
            "user_id": u.id,
            "user_email": u.email,
            "company_ids": [c.id for c in companies],
            "company_names": [c.name for c in companies],
        }


def main():
    fx = _seed_fixture()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1400, "height": 900},
            locale="ar-EG",
        )
        page = context.new_page()

        _login(page, fx["user_email"], "mce123!")

        # Sanity: after login the user is on their default active company.
        # We now cycle through all 3 explicitly.
        for idx, (cid, cname) in enumerate(
            zip(fx["company_ids"], fx["company_names"]), start=1
        ):
            page.goto(f"{BASE}/auth/switch-company/{cid}")
            page.wait_for_load_state("networkidle")

            page.goto(f"{BASE}/my/account")
            page.wait_for_load_state("networkidle")

            html = page.content()
            shot = SHOT / f"mce_{idx:02d}_{cname}.png"
            page.screenshot(path=shot, full_page=True)

            # The critical assertion — the redirect flash string is
            # exactly what Abdelhamid screenshotted. If it appears we
            # know the page bounced back to dashboard because
            # _my_employee() returned None.
            flash_line = "هذه الصفحة للموظفين المرتبطين بسجل HR فقط"
            assert flash_line not in html, (
                f"BUG PRESENT in company {cname} (id={cid}) — "
                f"page still says '{flash_line}'. See {shot}."
            )

            # Also assert the page actually rendered account content —
            # look for the page's own header keyword.
            assert ("حسابي" in html or "المعلومات الشخصية" in html
                    or "بياناتي" in html), (
                f"account page didn't render in {cname} (id={cid}). "
                f"See {shot}."
            )
            print(f"  ✓ {cname} (id={cid}): /my/account renders — {shot.name}")

        browser.close()

    print("\nAll 3 companies passed — the same user reaches /my/account in each.")


if __name__ == "__main__":
    main()
