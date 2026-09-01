#!/usr/bin/env python3
"""MARSOUD-TKT-ADMIN-VOID-SAAS-INVOICE (Abdelhamid 2026-08-31) —
delete/cancel a SaaS invoice straight from /admin/saas without
having to switch-into company 8 (Manasety) and open its invoice
list. Same behaviour as the tenant-side invoices.delete route
because both now call the shared services.invoicing.void_invoice.

Checks:
  1. services.invoicing.void_invoice exists with the expected
     signature and docstring.
  2. void_invoice(DRAFT invoice) hard-deletes the row.
  3. void_invoice(posted invoice) sets status=VOIDED + voided_at +
     void_reason and creates a reversing refund.
  4. void_invoice(already-voided) raises RuntimeError instead of
     double-reversing.
  5. Super-admin route /admin/saas/invoices/<id>/void is registered
     POST-only + superadmin-gated (viewer/owner cannot access).
  6. Route rejects a blank reason with a validation flash — the
     tenant-side default of "حذف الفاتورة" does NOT kick in here.
  7. Route on success stamps status=VOIDED + void_reason + creates
     the reversing refund (proves the shortcut is byte-identical
     to the tenant-side delete).
  8. Template has the trigger button, modal, form, and JS hook.
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


def _strip_comments(src):
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


def _boot_fixture(prefix, role):
    """Same helper as other MARSOUD-TKT audits — creates a Plan +
    Company + one user in `role`. Returns (email, cid, uid)."""
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
            cols = {c["name"] for c in insp.get_columns(t.name)}
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

    try:
        from app.services.legal import get_terms_version
        tv = get_terms_version() or "audit"
    except Exception:
        tv = "audit"

    u = User(email=f"user__{prefix.lower()}__@x.io",
             full_name=f"User {prefix}",
             is_active=True, email_verified_at=datetime.utcnow(),
             terms_version=tv, terms_accepted_at=datetime.utcnow())
    u.set_password("pw12345678")
    db.session.add(u); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=u.id, company_id=c.id, role=role))
    db.session.commit()
    return u.email, c.id, u.id


def _teardown(prefix):
    from sqlalchemy import text, inspect
    from app import db
    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        f"SELECT id FROM companies WHERE name LIKE '__{prefix}__%'"))]
    for cid in cids:
        for t in reversed(db.metadata.sorted_tables):
            cols = {c["name"] for c in insp.get_columns(t.name)}
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


def _make_posted_invoice(cid, uid):
    """Create a customer + posted invoice ready for voiding."""
    from datetime import date, timedelta
    from decimal import Decimal
    from app import db
    from app.models import (
        Customer, Invoice, InvoiceItem, InvoiceStatus,
    )
    from app.services.subsidiary import ensure_customer_account
    from app.services.invoicing import post_invoice_to_ledger

    cust = Customer(company_id=cid, name="عميل الحذف")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)

    inv = Invoice(
        company_id=cid, number="INV-VOID-01",
        customer_id=cust.id,
        issue_date=date.today(),
        due_date=date.today() + timedelta(days=30),
        currency="EGP", tax_rate=Decimal("0"),   # no VAT complications
        status=InvoiceStatus.DRAFT,
        created_by_id=uid,
    )
    db.session.add(inv); db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, company_id=cid,
        description="خدمة اختبار",
        quantity=Decimal("1"), unit_price=Decimal("100"),
        line_total=Decimal("100"),
    ))
    inv.recalc()
    inv.status = InvoiceStatus.SENT
    post_invoice_to_ledger(inv, created_by=uid)
    db.session.commit()
    return inv


@check("1. services.invoicing.void_invoice exists with the right signature")
def _():
    from app.services.invoicing import void_invoice
    import inspect as _inspect
    sig = _inspect.signature(void_invoice)
    params = list(sig.parameters)
    assert params == ["invoice", "reason", "actor_id"], \
        f"expected (invoice, reason, actor_id); got {params}"
    doc = (void_invoice.__doc__ or "").lower()
    assert "draft" in doc, "docstring should describe the DRAFT branch"
    assert "voided" in doc, "docstring should describe the VOIDED branch"
    return "signature + docstring correct"


@check("2. void_invoice(DRAFT) hard-deletes the row")
def _():
    from datetime import date, timedelta
    from decimal import Decimal
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import Customer, Invoice, InvoiceStatus
        from app.services.invoicing import void_invoice
        from app.services.subsidiary import ensure_customer_account

        email, cid, uid = _boot_fixture("VOID_DR", "owner")
        try:
            cust = Customer(company_id=cid, name="عميل DRAFT")
            db.session.add(cust); db.session.flush()
            ensure_customer_account(cust)
            inv = Invoice(
                company_id=cid, number="INV-VD-01",
                customer_id=cust.id,
                issue_date=date.today(),
                due_date=date.today() + timedelta(days=30),
                currency="EGP", tax_rate=Decimal("0"),
                status=InvoiceStatus.DRAFT,
                created_by_id=uid,
            )
            db.session.add(inv); db.session.commit()
            inv_id = inv.id

            outcome = void_invoice(inv, "test reason", uid)
            assert outcome == "deleted", \
                f"DRAFT should return 'deleted'; got {outcome!r}"
            assert db.session.get(Invoice, inv_id) is None, \
                "DRAFT invoice should be hard-deleted (row gone)"
            return "DRAFT → hard delete"
        finally:
            _teardown("VOID_DR")


@check("3. void_invoice(posted) → VOIDED + refund reversal + void_reason")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import Invoice, InvoiceStatus, Refund
        from app.services.invoicing import void_invoice

        email, cid, uid = _boot_fixture("VOID_POST", "owner")
        try:
            inv = _make_posted_invoice(cid, uid)
            inv_id = inv.id

            outcome = void_invoice(inv, "العميل لغى الاشتراك", uid)
            assert outcome == "voided", \
                f"posted invoice should return 'voided'; got {outcome!r}"

            # Re-fetch to be sure the DB row reflects the change
            fresh = db.session.get(Invoice, inv_id)
            assert fresh.status == InvoiceStatus.VOIDED, \
                f"status should be VOIDED; got {fresh.status}"
            assert fresh.voided_at is not None, \
                "voided_at timestamp missing"
            assert fresh.voided_by_id == uid, \
                f"voided_by_id should be {uid}; got {fresh.voided_by_id}"
            assert fresh.void_reason == "العميل لغى الاشتراك", \
                f"void_reason not saved; got {fresh.void_reason!r}"

            # And a Refund row exists — that's the "المرتجعات" the
            # ticket promises will show the deleted invoice
            refund = Refund.query.filter_by(invoice_id=inv_id).first()
            assert refund is not None, \
                "issue_refund should have created a Refund row"
            return "VOIDED + refund + void_reason all set"
        finally:
            _teardown("VOID_POST")


@check("4. void_invoice(already voided) → RuntimeError")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import InvoiceStatus
        from app.services.invoicing import void_invoice

        email, cid, uid = _boot_fixture("VOID_TWICE", "owner")
        try:
            inv = _make_posted_invoice(cid, uid)
            void_invoice(inv, "first void", uid)
            # Second call must complain, not silently double-reverse
            try:
                void_invoice(inv, "second void", uid)
            except RuntimeError as e:
                assert "معكوسة" in str(e) or "ملغاة" in str(e), \
                    f"error should mention the state; got: {e}"
                return "double-void refused"
            raise AssertionError("second void_invoice should have raised")
        finally:
            _teardown("VOID_TWICE")


@check("5. /admin/saas/invoices/<id>/void registered POST-only + superadmin-gated")
def _():
    from app import create_app
    app = create_app()
    rule = None
    for r in app.url_map.iter_rules():
        if r.endpoint == "superadmin.saas_void_invoice":
            rule = r
            break
    assert rule, "superadmin.saas_void_invoice endpoint not registered"
    methods = set(rule.methods or []) - {"HEAD", "OPTIONS"}
    assert methods == {"POST"}, f"expected POST-only; got {methods}"

    # And the source should have @superadmin_required decorator
    src = _read("app/routes/superadmin.py")
    m = re.search(
        r"@superadmin_required[^d]*def saas_void_invoice",
        src, re.DOTALL)
    assert m, "saas_void_invoice must be @superadmin_required"
    return "POST-only + superadmin_required decorator"


@check("6. blank reason → validation error, invoice untouched")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import Invoice, InvoiceStatus

        email, cid, uid = _boot_fixture("VOID_BLANK", "owner")
        try:
            # We need a super-admin flag on this user for the route
            # to pass. Just flip it on the fixture user.
            from app.models import User
            u = db.session.get(User, uid)
            u.is_superadmin = True
            db.session.commit()

            inv = _make_posted_invoice(cid, uid)
            inv_id = inv.id

            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.post(
                    f"/admin/saas/invoices/{inv_id}/void",
                    data={"reason": "   "},   # only whitespace
                    follow_redirects=False)
                # Redirect back to /admin/saas after flashing an error
                assert r.status_code in (302, 303)

            fresh = db.session.get(Invoice, inv_id)
            assert fresh.status != InvoiceStatus.VOIDED, \
                "invoice was voided despite blank reason"
            assert fresh.void_reason is None, \
                f"void_reason set on failed submission: {fresh.void_reason!r}"
            return "blank reason blocked"
        finally:
            _teardown("VOID_BLANK")


@check("7. valid reason → VOIDED + Refund + audit log row")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        from app.models import (
            Invoice, InvoiceStatus, Refund, PlatformAuditLog, User,
        )

        email, cid, uid = _boot_fixture("VOID_OK", "owner")
        try:
            u = db.session.get(User, uid)
            u.is_superadmin = True
            db.session.commit()

            inv = _make_posted_invoice(cid, uid)
            inv_id = inv.id

            with app.test_client() as client:
                client.post("/login", data={
                    "email": email, "password": "pw12345678"})
                r = client.post(
                    f"/admin/saas/invoices/{inv_id}/void",
                    data={"reason": "العميل قرر مش يكمل"},
                    follow_redirects=False)
                assert r.status_code in (302, 303)

            fresh = db.session.get(Invoice, inv_id)
            assert fresh.status == InvoiceStatus.VOIDED, \
                f"status should be VOIDED after admin void; got {fresh.status}"
            assert fresh.void_reason == "العميل قرر مش يكمل", \
                "void_reason not persisted from admin route"

            # Refund row exists (the ticket's "المرتجعات" promise)
            refund = Refund.query.filter_by(invoice_id=inv_id).first()
            assert refund is not None, \
                "Refund not created from admin void — invoice would " \
                "not appear in the refunds list"

            # Audit trail
            audit = PlatformAuditLog.query.filter_by(
                action="saas.invoice_voided").order_by(
                PlatformAuditLog.id.desc()).first()
            assert audit is not None, \
                "platform_audit_logs missing the saas.invoice_voided row"
            assert audit.target_company_id == cid, \
                "audit row missing target_company_id"
            return "end-to-end: VOIDED + Refund + audit all landed"
        finally:
            _teardown("VOID_OK")


@check("8. template has trigger button + modal + JS wiring")
def _():
    src = _strip_comments(_read("app/templates/admin/saas_index.html"))
    # Trigger
    assert "__openVoidPopup" in src, \
        "trigger button JS entry point missing"
    assert "🗑" in src or "حذف/إلغاء" in src, \
        "delete/cancel button label missing"
    # Modal
    assert 'id="void-invoice-modal"' in src, \
        "modal container missing"
    assert 'name="reason"' in src, \
        "reason textarea name missing"
    assert 'required' in src, \
        "reason must be a required field"
    # Form action pattern
    assert "/admin/saas/invoices/" in src, \
        "form action must build /admin/saas/invoices/<id>/void URL"
    # 2026-08-31 regression: the earlier onclick + |tojson pattern
    # broke because Jinja's autoescape treats |tojson output as
    # Markup (safe), so |e was a no-op and the literal `"` in
    # `"SAAS-001"` closed the HTML attribute early — the click looked
    # wired but the modal never opened. Fix: data-* attributes get
    # proper autoescape from Jinja + a JS delegated click listener.
    assert 'data-void-invoice-id=' in src, \
        "trigger button must expose the invoice id via a data-* " \
        "attribute (see JS delegated listener). onclick+tojson had " \
        "a known escape bug — do NOT re-introduce it."
    assert 'data-void-invoice-number=' in src, \
        "trigger button must expose the invoice number via data-*"
    assert 'js-void-invoice' in src, \
        "trigger button needs the .js-void-invoice class so the " \
        "delegated listener picks it up"
    assert "closest('.js-void-invoice')" in src, \
        "JS listener must delegate off the .js-void-invoice class"
    return "button + modal + delegated data-attr wiring all present"


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
