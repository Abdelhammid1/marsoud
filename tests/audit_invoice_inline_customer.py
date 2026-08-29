#!/usr/bin/env python3
"""MARSOUD-TKT-INVOICE-INLINE-CUSTOMER (Abdelhamid 2026-08-29) —
quick-add-customer modal on the invoice-new page.

A bookkeeper writing a new invoice for a walk-in customer used to
have to leave the invoice draft, go to /customers/new, come back,
and start over. This ticket adds an inline modal on invoices/form.html
that POSTs to /customers/quick-create (JSON) and injects the new
option into the invoice's customer <select> without a navigation.

Checks:
  1. Route customers.quick_create is registered as POST-only.
  2. Route is permission-gated on partners.manage (same as
     customers.new — creating a customer is a write action).
  3. Invoice form template carries the trigger + modal + JS hook,
     all gated on has_permission('partners.manage').
  4. End-to-end: authenticated owner POSTs to quick-create, gets
     200 + JSON {ok: true, id, name}, and the Customer row lands
     in the DB with a subsidiary account.
  5. Missing name returns 400 + ok:false + Arabic error.
  6. Viewer role (no partners.manage) hitting quick-create gets 403.
  7. Editing an invoice loads the modal too (edit route passes
     `reps` context).
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


@check("1. customers.quick_create route registered POST-only")
def _():
    from app import create_app
    app = create_app()
    rule = None
    for r in app.url_map.iter_rules():
        if r.endpoint == "customers.quick_create":
            rule = r
            break
    assert rule, "customers.quick_create endpoint not registered"
    methods = set(rule.methods or []) - {"HEAD", "OPTIONS"}
    assert methods == {"POST"}, \
        f"quick_create should be POST-only; got {methods}"
    return f"{rule.rule} POST"


@check("2. quick_create gated on partners.manage (inline, JSON-friendly)")
def _():
    """The gate is INLINE inside quick_create, not the standard
    @require_permission decorator — because @require_permission
    responds to a denial with a 302 to /dashboard, which a JSON
    client can't follow. quick_create returns 403 + JSON error so
    the modal can render the message. Runtime check 6 exercises the
    denial path end-to-end; this one guards the source shape."""
    src = _read("app/routes/customers.py")
    m = re.search(
        r"def quick_create\(\):(.*?)(?=\ndef \w)",
        src, re.DOTALL,
    )
    assert m, "quick_create function not found"
    body = m.group(1)
    assert 'has_permission("partners.manage")' in body, \
        "quick_create must call has_permission('partners.manage') " \
        "inline — otherwise a viewer bypasses the write gate."
    assert "403" in body, \
        "quick_create should return HTTP 403 on permission denial " \
        "(not 302), so the modal's fetch() can surface the error."
    return "inline has_permission call + 403 return present"


@check("3. invoice form template carries trigger + modal + JS, all gated")
def _():
    src = _read("app/templates/invoices/form.html")
    # Trigger
    assert "btn-open-new-customer" in src, \
        "invoice form has no '+ عميل جديد' trigger button"
    # Modal container
    assert 'id="new-customer-modal"' in src, \
        "invoice form has no #new-customer-modal container"
    # Every relevant field the standalone customers/new form has
    for field_id in ("nc-name", "nc-phone", "nc-email", "nc-address",
                     "nc-tax", "nc-rep", "nc-commission"):
        assert f'id="{field_id}"' in src, \
            f"modal missing field: {field_id}"
    # JS hook posts to the quick-create endpoint
    assert "customers.quick_create" in src, \
        "modal JS does not resolve customers.quick_create endpoint"
    # Permission gate wraps the modal + trigger
    assert src.count("has_permission('partners.manage')") >= 2, \
        "trigger and modal should both be behind " \
        "has_permission('partners.manage') — otherwise a viewer " \
        "sees a button that always 403s"
    return "trigger + modal + JS + permission gates all present"


def _boot_fixture(prefix, role_name):
    """Set up a minimal signed-in-user fixture that passes every
    global before_request gate (plan selection + terms consent).
    Returns (app, client, company_id) — caller uses it inside an
    `app.app_context()` already open."""
    from datetime import datetime
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa
    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        f"SELECT id FROM companies WHERE name LIKE '__{prefix}__%'"))]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        f"DELETE FROM users WHERE email LIKE '%__{prefix.lower()}__%'"))
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="Audit", name_ar="تدقيق",
                    allowed_subitems=None)
        plan.set_modules(["accounting", "sales", "settings"])
        db.session.add(plan); db.session.flush()

    c = Company(name=f"__{prefix}__co", base_currency="EGP",
                subdomain=prefix.lower(), plan_id=plan.id,
                subscription_started_at=datetime.utcnow(),
                subscription_expires_at=datetime(2999, 1, 1))
    db.session.add(c); db.session.commit()
    seed_default_coa(c.id); db.session.commit()

    # Terms version — user must match current or they get shunted
    # to /re-accept-terms before ever reaching our route.
    try:
        from app.services.legal import get_terms_version
        terms_v = get_terms_version() or "audit"
    except Exception:
        terms_v = "audit"

    u = User(email=f"user__{prefix.lower()}__@x.io",
             full_name=f"User {prefix}",
             is_active=True, email_verified_at=datetime.utcnow(),
             terms_version=terms_v, terms_accepted_at=datetime.utcnow())
    u.set_password("pw12345678")
    db.session.add(u); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role=role_name))
    db.session.commit()
    return u.email, c.id


def _teardown_fixture(prefix):
    from sqlalchemy import text, inspect
    from app import db
    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        f"SELECT id FROM companies WHERE name LIKE '__{prefix}__%'"))]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {col["name"] for col in insp.get_columns(t.name)}
            if "company_id" in cols:
                db.session.execute(text(
                    f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        f"DELETE FROM users WHERE email LIKE '%__{prefix.lower()}__%'"))
    db.session.commit()


@check("4. end-to-end: owner POST → 200 JSON + Customer row created")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import Customer, Account
        email, cid = _boot_fixture("QC_AUDIT", "owner")

        try:
            with app.test_client() as client:
                r = client.post("/login", data={
                    "email": email, "password": "pw12345678",
                }, follow_redirects=False)
                assert r.status_code in (200, 302, 303), \
                    f"login failed: {r.status_code}"
                r = client.post("/customers/quick-create", data={
                    "name": "عميل الاختبار السريع",
                    "phone": "+201000000000",
                    "email": "walkin@example.com",
                    "tax_number": "300-999-000",
                }, headers={"X-Requested-With": "XMLHttpRequest"})
                assert r.status_code == 200, \
                    f"quick_create returned {r.status_code}: {r.data[:200]!r}"
                data = r.get_json()
                assert data and data.get("ok") is True, f"bad json: {data}"
                assert data.get("id"), "no id returned"
                assert data.get("name") == "عميل الاختبار السريع", \
                    f"name round-tripped wrong: {data.get('name')!r}"

                cust = db.session.get(Customer, data["id"])
                assert cust and cust.company_id == cid, \
                    "customer row not persisted under the active company"
                assert cust.phone == "+201000000000"
                assert cust.email == "walkin@example.com"
                assert cust.tax_number == "300-999-000"

                assert cust.account_id, \
                    "ensure_customer_account did not run — the trial " \
                    "balance would miss this customer"
                sub = db.session.get(Account, cust.account_id)
                assert sub and sub.code.startswith("1130-"), \
                    f"customer.account_id points at the wrong account: {sub.code if sub else None}"
            return f"owner posted customer id={data['id']} + subsidiary opened"
        finally:
            _teardown_fixture("QC_AUDIT")


@check("5. missing name returns 400 + Arabic error")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, _ = _boot_fixture("QC_V", "owner")
        try:
            with app.test_client() as client:
                client.post("/login", data={"email": email, "password": "pw12345678"})
                r = client.post("/customers/quick-create", data={"name": "   "})
                assert r.status_code == 400, f"expected 400, got {r.status_code}"
                data = r.get_json()
                assert data and data.get("ok") is False, f"bad json: {data}"
                assert "الاسم" in (data.get("error") or ""), \
                    f"error should mention الاسم; got {data.get('error')!r}"
            return "blank name → 400 with Arabic error"
        finally:
            _teardown_fixture("QC_V")


@check("6. viewer without partners.manage → 403")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        # Viewer isn't the owner, so the plan gate skips them entirely —
        # a plan_id on the company is still fine, keeps the fixture
        # uniform with checks 4 + 5.
        email, _ = _boot_fixture("QC_R", "viewer")
        try:
            with app.test_client() as client:
                client.post("/login", data={"email": email, "password": "pw12345678"})
                r = client.post("/customers/quick-create", data={"name": "X"})
                assert r.status_code == 403, \
                    f"viewer should get 403, got {r.status_code}"
            return "viewer role blocked by partners.manage gate"
        finally:
            _teardown_fixture("QC_R")


@check("7. edit route also passes `reps` context (modal works in edit view)")
def _():
    """The modal's rep dropdown depends on the `reps` variable in
    the template — if the edit route stops passing it, the rep
    dropdown silently falls back to `— بدون —` only. Guard against
    that here since the two calls are 20 lines apart in invoices.py."""
    src = _read("app/routes/invoices.py")
    # Both render_template calls for invoices/form.html must pass reps.
    calls = re.findall(
        r'render_template\("invoices/form\.html"[^)]*\)',
        src, re.DOTALL,
    )
    assert len(calls) == 2, \
        f"expected 2 render_template invoices/form calls, found {len(calls)}"
    for c in calls:
        assert "reps=" in c, \
            f"render_template call missing reps=: {c[:120]}..."
    return "new + edit both pass reps"


def main():
    passed = failed = 0
    for label, fn in CHECKS:
        try:
            res = fn()
            print(f"PASS  {label}  ⇒ {res}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL  {label}  ⇒ {type(e).__name__}: {e}")
            failed += 1
            import traceback; traceback.print_exc()
    print()
    print(f"────  {passed} passed, {failed} failed  ────")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
