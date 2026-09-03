#!/usr/bin/env python3
"""MARSOUD-TKT-HR-DECISIONS-03-PRINTABLE-FORM (2026-09-03).

Official PDF document for HR decisions — clone of the payslip PDF
pattern (WeasyPrint primary + ReportLab legacy fallback).

Checks:
  1. Route /hr/decisions/<id>/pdf registered, gated by payroll.view.
  2. Static: export_hr_decision_pdf() calls _weasyprint_render +
     _export_hr_decision_pdf_legacy (mirrors
     audit_pdf_payroll_weasyprint.py:4).
  3. ADMIN happy path (PROMOTION) → PDF renders — magic bytes +
     Amiri font embedded + size floor.
  4. FINANCIAL PENDING_PAYROLL → PDF renders. Confirms the
     "سيُطوى في كشف الراتب القادم" branch doesn't blow up on a
     row with no journal_entry_id / payroll_run_id.
  5. HTTP: owner GET returns 200 + application/pdf + %PDF-magic.
  6. Cross-tenant: owner of tenant B → 404 on tenant A's dec.
  7. Fallback: monkeypatch _weasyprint_render to raise → the
     ReportLab legacy path still returns a valid %PDF body.
"""
import os
import re
import sys
from pathlib import Path
from datetime import datetime, date, timedelta
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


# ─── Static-check helpers (from audit_pdf_payroll_weasyprint.py:44-62)
def _read(relpath):
    return (ROOT / relpath).read_text(encoding="utf-8")


def _strip_comments(src):
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


def _extract_body(src, func_name):
    m = re.search(
        r"^def " + re.escape(func_name) + r"\([^)]*\):\n(.*?)(?=^def \w)",
        src, re.MULTILINE | re.DOTALL,
    )
    assert m, f"{func_name} not found in services/export.py"
    return m.group(1)


# ─── Setup helpers (from audit_hr_decisions.py:_boot, _make_employee)
def _boot(prefix):
    from sqlalchemy import text, inspect
    from app import db
    from app.models import Company, User, Plan
    from app.models.user import user_companies
    from app.services.seed_coa import seed_default_coa

    insp = inspect(db.engine)
    cids = [r[0] for r in db.session.execute(text(
        "SELECT id FROM companies WHERE name LIKE :p"),
        {"p": f"__{prefix}__%"})]
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
        "DELETE FROM users WHERE email LIKE :p"),
        {"p": f"%__{prefix.lower()}__%"})
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
    return owner.email, c.id, owner.id


def _make_employee(cid, name="أحمد", basic_salary=3000):
    from app import db
    from app.models import Employee, EmployeeStatus
    from app.services.subsidiary import ensure_employee_account
    emp = Employee(company_id=cid, name=name, job_title="محاسب",
                    basic_salary=Decimal(str(basic_salary)),
                    status=EmployeeStatus.ACTIVE, is_active=True,
                    start_date=date.today() - timedelta(days=365))
    db.session.add(emp); db.session.flush()
    ensure_employee_account(emp)
    db.session.commit()
    return emp


def _make_promotion(cid, emp, oid):
    from app.services.hr_decisions import create_decision, execute_decision
    dec = create_decision(cid, employee_id=emp.id, kind="PROMOTION",
        effective_date=date.today(), title="ترقية إلى مدير",
        body="أداء ممتاز خلال العام المالي.",
        actor_id=oid)
    execute_decision(dec, actor_id=oid)
    return dec


def _make_pending_bonus(cid, emp, oid, amount=500):
    from app.services.hr_decisions import create_decision, execute_decision
    dec = create_decision(cid, employee_id=emp.id, kind="BONUS",
        effective_date=date.today(), title="مكافأة إنجاز",
        timing="NEXT_PAYROLL", amount=amount,
        actor_id=oid)
    execute_decision(dec, actor_id=oid)
    return dec


@check("1. Route registered + gated by payroll.view")
def _():
    from app import create_app
    app = create_app()
    names = {r.endpoint for r in app.url_map.iter_rules()}
    assert "hr_decisions.pdf" in names, "endpoint not registered"
    # Static grep: same decorator stack as detail().
    src = _read("app/routes/hr_decisions.py")
    stripped = _strip_comments(src)
    assert re.search(
        r'@bp\.route\("/<int:dec_id>/pdf"\)[^\n]*\n'
        r'@login_required[^\n]*\n'
        r'@require_permission\("payroll\.view"\)[^\n]*\n'
        r'def pdf\(', stripped), \
        "decorator stack on pdf() diverges from detail()"
    return "hr_decisions.pdf registered with @require_permission(payroll.view)"


@check("2. export_hr_decision_pdf calls _weasyprint_render + "
        "_export_hr_decision_pdf_legacy")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    body = _extract_body(src, "export_hr_decision_pdf")
    assert "_weasyprint_render(" in body, \
        "export_hr_decision_pdf no longer calls _weasyprint_render"
    assert '"pdfs/hr_decision.html"' in body, \
        "template path missing"
    assert "_export_hr_decision_pdf_legacy" in body, \
        "fallback call missing"
    assert re.search(
        r"^def _export_hr_decision_pdf_legacy\(", src, re.MULTILINE), \
        "_export_hr_decision_pdf_legacy definition missing"
    return "WeasyPrint primary + ReportLab fallback both wired"


@check("3. ADMIN happy path (PROMOTION) → PDF renders")
def _():
    from app import create_app, db
    from app.services.export import export_hr_decision_pdf
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HRPDF3")
        emp = _make_employee(cid, "أحمد شلبي")
        dec = _make_promotion(cid, emp, oid)
        # Warm the lazy relationships (audit_pdf_payroll_
        # weasyprint.py:228-231 pattern).
        db.session.expire_all()
        from app.models import HrDecision
        dec = db.session.get(HrDecision, dec.id)
        _ = dec.employee.name
        buf = export_hr_decision_pdf(dec)
        data = buf.read()
        assert data.startswith(b"%PDF"), \
            f"not a PDF: {data[:20]!r}"
        assert b"Amiri" in data, "Amiri font not embedded"
        assert len(data) >= 4096, f"suspiciously small: {len(data)}"
        return f"PDF {len(data)} bytes, Amiri embedded"


@check("4. FINANCIAL PENDING_PAYROLL → PDF renders (no fake JE #)")
def _():
    from app import create_app, db
    from app.services.export import export_hr_decision_pdf
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HRPDF4")
        emp = _make_employee(cid, "منى")
        dec = _make_pending_bonus(cid, emp, oid, amount=500)
        assert dec.status == "PENDING_PAYROLL"
        assert dec.journal_entry_id is None
        assert dec.payroll_run_id is None
        db.session.expire_all()
        from app.models import HrDecision
        dec = db.session.get(HrDecision, dec.id)
        _ = dec.employee.name
        buf = export_hr_decision_pdf(dec)
        data = buf.read()
        assert data.startswith(b"%PDF")
        assert len(data) >= 4096
        return "PENDING_PAYROLL BONUS renders as valid PDF"


@check("5. HTTP: owner GET → 200 + application/pdf + %PDF magic")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HRPDF5")
        emp = _make_employee(cid, "خالد")
        dec = _make_promotion(cid, emp, oid)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(oid)
            sess["_fresh"] = True
            sess["active_company_id"] = cid
        r = client.get(f"/hr/decisions/{dec.id}/pdf")
        assert r.status_code == 200, \
            f"got {r.status_code}: {r.get_data(as_text=True)[:200]}"
        assert r.mimetype == "application/pdf", \
            f"mimetype={r.mimetype}"
        assert r.data.startswith(b"%PDF"), \
            f"body: {r.data[:20]!r}"
        return f"200 application/pdf, {len(r.data)} bytes"


@check("6. Cross-tenant → 404")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        # Tenant A + its decision.
        email_a, cid_a, oid_a = _boot("HRPDF6A")
        emp_a = _make_employee(cid_a, "علي")
        dec_a = _make_promotion(cid_a, emp_a, oid_a)
        # Tenant B — separate owner.
        email_b, cid_b, oid_b = _boot("HRPDF6B")
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(oid_b)
            sess["_fresh"] = True
            sess["active_company_id"] = cid_b
        r = client.get(f"/hr/decisions/{dec_a.id}/pdf")
        assert r.status_code == 404, \
            f"cross-tenant leaked: {r.status_code}"
        return "owner-B → 404 on decision from tenant A"


@check("7. Fallback: monkeypatch _weasyprint_render to raise → "
        "ReportLab legacy path still returns a valid PDF")
def _():
    from app import create_app, db
    from app.services import export as export_mod
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("HRPDF7")
        emp = _make_employee(cid, "ياسمين")
        dec = _make_promotion(cid, emp, oid)
        db.session.expire_all()
        from app.models import HrDecision
        dec = db.session.get(HrDecision, dec.id)
        _ = dec.employee.name
        # Force the fallback by making _weasyprint_render raise.
        original = export_mod._weasyprint_render
        def _boom(*a, **kw):
            raise RuntimeError("no libpango — simulated for audit")
        export_mod._weasyprint_render = _boom
        try:
            buf = export_mod.export_hr_decision_pdf(dec)
        finally:
            export_mod._weasyprint_render = original
        data = buf.read()
        assert data.startswith(b"%PDF"), \
            f"legacy fallback didn't produce a PDF: {data[:20]!r}"
        assert len(data) >= 1024, \
            f"legacy fallback suspiciously small: {len(data)}"
        return f"ReportLab legacy produced {len(data)} bytes"


def main():
    from app import create_app
    _ = create_app()
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
