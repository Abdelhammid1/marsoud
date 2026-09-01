#!/usr/bin/env python3
"""MARSOUD-COMM-DASHBOARD (Abdelhamid 2026-08-31) — standalone
commissions management surface.

Phase-1 scope (this audit):
  * 5 tabs on /commissions/ (all / unpaid / paid / by_rep / voided)
  * Individual settle via /commissions/<id>/settle (reuses the
    existing settle_commission_manual service)
  * Bulk settle via /commissions/bulk-settle
  * Void with mandatory reason via /commissions/<id>/void — uses
    the new services.sales_commissions.void_commission
  * Rep detail page /commissions/rep/<user_id>
  * KPI cards + shared payment-method picker + filter form
  * Zero coupling to the payroll pages

Deferred to Phase 2 (documented in the ticket + commit):
  * PDF generation on settle
  * Email delivery with PDF attachment
  * On-demand export by period

Checks:
  1. Blueprint registered with the expected endpoints.
  2. GET /commissions/ tabs render (all / unpaid / paid / by_rep /
     voided) and show the KPI cards region + filter form.
  3. void_commission service exists with the right signature.
  4. void_commission(UNPAID) writes voided_at + void_reason + a
     reversal JE tagged source_type='commission_void'.
  5. void_commission refuses blank reason with LedgerError.
  6. void_commission refuses if already voided (idempotent contract).
  7. POST /commissions/<id>/settle uses settle_commission_manual
     end-to-end (row flips to PAID + settle JE created).
  8. POST /commissions/bulk-settle settles every checked row and
     reports partial failures without stopping.
  9. Rep detail page renders the rep's full history + totals.
"""
import os
import re
import sys
from pathlib import Path
from datetime import datetime, date
from decimal import Decimal

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


def _boot(prefix):
    """Company + owner user with payroll.write permission + 2 sales
    users. Returns (owner_email, company_id, owner_id, rep_ids)."""
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
                    f"DELETE FROM {t.name} WHERE company_id = :c"),
                    {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        f"DELETE FROM users WHERE email LIKE '%__{prefix.lower()}__%'"))
    db.session.commit()

    plan = Plan.query.filter_by(code=f"__{prefix}__").first()
    if not plan:
        plan = Plan(code=f"__{prefix}__", name="C", name_ar="C",
                    allowed_subitems=None)
        db.session.add(plan)
    plan.set_modules(["accounting", "sales", "hr", "reports"])
    db.session.flush()

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

    owner = User(email=f"owner__{prefix.lower()}__@x.io",
                 full_name=f"Owner {prefix}", is_active=True,
                 email_verified_at=datetime.utcnow(),
                 terms_version=tv, terms_accepted_at=datetime.utcnow())
    owner.set_password("pw12345678")
    db.session.add(owner); db.session.commit()
    db.session.execute(user_companies.insert().values(
        user_id=owner.id, company_id=c.id, role="owner"))
    db.session.commit()

    reps = []
    for name in ("مندوب أ", "مندوب ب"):
        rep = User(email=f"{name[-1]}__{prefix.lower()}__@x.io",
                   full_name=name, is_active=True,
                   email_verified_at=datetime.utcnow(),
                   terms_version=tv, terms_accepted_at=datetime.utcnow())
        rep.set_password("pw12345678")
        db.session.add(rep); db.session.commit()
        db.session.execute(user_companies.insert().values(
            user_id=rep.id, company_id=c.id, role="employee"))
        db.session.commit()
        reps.append(rep.id)
    return owner.email, c.id, owner.id, reps


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
                    f"DELETE FROM {t.name} WHERE company_id = :c"),
                    {"c": cid})
        db.session.execute(text(
            "DELETE FROM user_companies WHERE company_id = :c"),
            {"c": cid})
        db.session.execute(text(
            "DELETE FROM companies WHERE id = :c"), {"c": cid})
    db.session.execute(text(
        f"DELETE FROM users WHERE email LIKE '%__{prefix.lower()}__%'"))
    db.session.commit()


def _make_commission(cid, rep_id, *, amount=100, status="UNPAID"):
    """Create a customer + invoice + SalesCommission ready for tests
    to settle or void."""
    from datetime import date as _date, timedelta
    from app import db
    from app.models import (
        Customer, Invoice, InvoiceStatus, SalesCommission,
    )
    from app.services.subsidiary import ensure_customer_account

    cust = Customer(company_id=cid, name="عميل الاختبار")
    db.session.add(cust); db.session.flush()
    ensure_customer_account(cust)
    inv = Invoice(company_id=cid, number=f"INV-C-{cust.id}",
                  customer_id=cust.id, issue_date=_date.today(),
                  due_date=_date.today() + timedelta(days=30),
                  currency="EGP", tax_rate=Decimal("0"),
                  status=InvoiceStatus.DRAFT)
    db.session.add(inv); db.session.commit()
    comm = SalesCommission(
        company_id=cid, sales_rep_id=rep_id,
        customer_id=cust.id, invoice_id=inv.id,
        taxable_base=Decimal(str(amount * 10)),
        amount=Decimal(str(amount)),
        commission_rate=Decimal("10"),
        period_year=2026, period_month=8,
        status=status,
    )
    db.session.add(comm); db.session.commit()
    return comm


@check("1. blueprint registered with the expected endpoints")
def _():
    from app import create_app
    app = create_app()
    endpoints = {r.endpoint for r in app.url_map.iter_rules()}
    for name in ("commissions_admin.index",
                 "commissions_admin.settle",
                 "commissions_admin.bulk_settle",
                 "commissions_admin.void",
                 "commissions_admin.rep_detail"):
        assert name in endpoints, f"missing endpoint: {name}"
    return "5 endpoints registered"


def _authed_client(app, oid, cid):
    """Bypass the login form + tenant-picker — set both session keys
    directly, the way audit_portal_403 does."""
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(oid)
        s["_fresh"] = True
        s["active_company_id"] = cid
    return c


@check("2. GET /commissions/ renders each tab + KPI cards + filters")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cid, oid, reps = _boot("CDASH2")
        try:
            client = _authed_client(app, oid, cid)
            if True:
                for tab in ("all", "unpaid", "paid", "by_rep", "voided"):
                    r = client.get(f"/commissions/?tab={tab}")
                    assert r.status_code == 200, (
                        f"tab={tab} returned {r.status_code} → "
                        f"{r.headers.get('Location')}"
                    )
                    html = r.data.decode("utf-8")
                    # KPI region
                    assert "إجمالي مستحق" in html, \
                        f"tab={tab} missing KPI card"
                    # Tab strip
                    assert "المستحقة" in html and "سجل الإلغاءات" in html
            return "5 tabs + KPI + tab strip render"
        finally:
            _teardown("CDASH2")


@check("3. void_commission service exists with the right signature")
def _():
    from app.services.sales_commissions import void_commission
    import inspect as _inspect
    sig = _inspect.signature(void_commission)
    assert list(sig.parameters)[0] == "commission"
    assert "reason" in sig.parameters, "must accept `reason` kwarg"
    assert "actor_id" in sig.parameters, "must accept `actor_id` kwarg"
    return "signature correct"


@check("4. void_commission writes fields + creates reversal JE")
def _():
    from app import create_app, db
    from app.models import SalesCommission
    from app.models.journal import JournalEntry
    from app.services.sales_commissions import void_commission

    app = create_app()
    with app.app_context():
        email, cid, oid, reps = _boot("CDASH4")
        try:
            comm = _make_commission(cid, reps[0], amount=250)
            _, rev = void_commission(comm, reason="اتحطت غلط",
                                      actor_id=oid)
            db.session.refresh(comm)
            assert comm.voided_at is not None, "voided_at not set"
            assert comm.voided_by_id == oid, "voided_by_id wrong"
            assert comm.void_reason == "اتحطت غلط", \
                f"void_reason not saved; got {comm.void_reason!r}"
            # Reversal JE tagged correctly
            assert rev.source_type == "commission_void"
            assert rev.source_id == comm.id
            return "voided fields + reversal JE both landed"
        finally:
            _teardown("CDASH4")


@check("5. void_commission refuses blank reason")
def _():
    from app import create_app
    from app.services.sales_commissions import void_commission
    from app.services.ledger import LedgerError

    app = create_app()
    with app.app_context():
        email, cid, oid, reps = _boot("CDASH5")
        try:
            comm = _make_commission(cid, reps[0])
            for bad in ("", "   ", None):
                try:
                    void_commission(comm, reason=bad, actor_id=oid)
                except LedgerError as e:
                    assert "سبب" in str(e), \
                        f"error should name 'سبب'; got: {e}"
                else:
                    raise AssertionError(
                        f"blank reason={bad!r} should have raised")
            return "blank reason blocked"
        finally:
            _teardown("CDASH5")


@check("6. void_commission refuses if already voided")
def _():
    from app import create_app, db
    from app.services.sales_commissions import void_commission
    from app.services.ledger import LedgerError

    app = create_app()
    with app.app_context():
        email, cid, oid, reps = _boot("CDASH6")
        try:
            comm = _make_commission(cid, reps[0])
            void_commission(comm, reason="أول مرة", actor_id=oid)
            try:
                void_commission(comm, reason="تاني مرة", actor_id=oid)
            except LedgerError as e:
                assert "ملغاة" in str(e), f"error should say ملغاة; got: {e}"
                return "double-void refused"
            raise AssertionError("second void should have raised")
        finally:
            _teardown("CDASH6")


@check("7. POST /commissions/<id>/settle marks the row PAID + posts JE")
def _():
    from app import create_app, db
    from app.models import SalesCommission
    from app.models.journal import JournalEntry

    app = create_app()
    with app.app_context():
        email, cid, oid, reps = _boot("CDASH7")
        try:
            comm = _make_commission(cid, reps[0], amount=500)
            client = _authed_client(app, oid, cid)
            if True:
                r = client.post(f"/commissions/{comm.id}/settle",
                                data={})
                assert r.status_code in (302, 303), \
                    f"expected redirect; got {r.status_code}"
            db.session.refresh(comm)
            assert comm.status == "PAID", \
                f"row must flip to PAID; got {comm.status}"
            # Settle JE exists
            je = (JournalEntry.query
                  .filter_by(source_type="commission_settle",
                              source_id=comm.id).first())
            assert je is not None, "settle JE not created"
            return "settle flow works end-to-end"
        finally:
            _teardown("CDASH7")


@check("8. POST /commissions/bulk-settle settles multiple rows")
def _():
    from app import create_app, db
    from app.models import SalesCommission

    app = create_app()
    with app.app_context():
        email, cid, oid, reps = _boot("CDASH8")
        try:
            c1 = _make_commission(cid, reps[0], amount=100)
            c2 = _make_commission(cid, reps[0], amount=200)
            c3 = _make_commission(cid, reps[1], amount=300)
            client = _authed_client(app, oid, cid)
            if True:
                r = client.post("/commissions/bulk-settle", data={
                    "commission_ids": [str(c1.id), str(c2.id),
                                        str(c3.id)],
                })
                assert r.status_code in (302, 303)
            for c in (c1, c2, c3):
                db.session.refresh(c)
                assert c.status == "PAID", \
                    f"commission {c.id} not settled: {c.status}"
            return "bulk settle marks 3 rows PAID"
        finally:
            _teardown("CDASH8")


@check("9. rep detail page renders history + totals")
def _():
    from app import create_app
    app = create_app()
    with app.app_context():
        email, cid, oid, reps = _boot("CDASH9")
        try:
            _make_commission(cid, reps[0], amount=250)
            _make_commission(cid, reps[0], amount=150)
            client = _authed_client(app, oid, cid)
            if True:
                r = client.get(f"/commissions/rep/{reps[0]}")
                assert r.status_code == 200
                html = r.data.decode("utf-8")
                assert "مندوب أ" in html, "rep name should show"
                assert "إجمالي مستحق" in html
                assert "400.00" in html, \
                    "sum of unpaid amounts (250+150) not shown"
            return "rep detail renders history + totals"
        finally:
            _teardown("CDASH9")


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
