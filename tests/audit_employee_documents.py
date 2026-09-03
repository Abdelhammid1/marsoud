#!/usr/bin/env python3
"""MARSOUD-HR-EMPLOYEE-DOCS-01 (2026-09-03) — per-employee document
tracking + missing-paper report.

Checks:
  1. Schema: required_document_types + employee_documents tables +
     the six critical columns present; models importable.
  2. HTTP: create RDT as owner → row lands; duplicate name → flash
     error, no second row.
  3. missing_documents_report: mandatory RDT + ACTIVE employee with
     no doc → appears; after submit_document → empty.
  4. Expiry: has_expiry=True + default_validity_months auto-computes
     expiry_date; pushing it to yesterday flips the report reason
     to "منتهية الصلاحية".
  5. File upload via HTTP: multipart POST + a fake PDF FileStorage
     → DB row carries file_storage_key/size + file exists on disk;
     GET the file endpoint → 200 application/pdf.
  6. Cross-tenant guard: 404 on tenant A's employee id from tenant B
     — for BOTH submit POST and file GET.
  7. Deactivate an RDT → drops from the missing report + the
     required_types list for new submissions, but the historical
     EmployeeDocument row stays intact.
  8. TERMINATED employee excluded from the report.
"""
import io
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


# Boot scaffold copied from tests/audit_hr_decisions.py.
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


def _make_employee(cid, name="أحمد", basic_salary=3000,
                    status_val=None):
    from app import db
    from app.models import Employee, EmployeeStatus
    from app.services.subsidiary import ensure_employee_account
    emp = Employee(company_id=cid, name=name, job_title="محاسب",
                    basic_salary=Decimal(str(basic_salary)),
                    status=status_val or EmployeeStatus.ACTIVE,
                    is_active=True,
                    start_date=date.today() - timedelta(days=365))
    db.session.add(emp); db.session.flush()
    ensure_employee_account(emp)
    db.session.commit()
    return emp


def _make_type(cid, name_ar="بطاقة", **kwargs):
    from app import db
    from app.models import RequiredDocumentType
    kwargs.setdefault("is_mandatory", True)
    kwargs.setdefault("has_expiry", False)
    kwargs.setdefault("is_active", True)
    dt = RequiredDocumentType(
        company_id=cid, name_ar=name_ar, **kwargs)
    db.session.add(dt); db.session.commit()
    return dt


@check("1. schema present + models importable")
def _():
    from app import create_app, db
    from sqlalchemy import inspect
    app = create_app()
    with app.app_context():
        insp = inspect(db.engine)
        tabs = insp.get_table_names()
        assert "required_document_types" in tabs
        assert "employee_documents" in tabs
        cols = {c["name"] for c in insp.get_columns(
            "employee_documents")}
        for want in ("employee_id", "document_type_id", "status",
                     "submitted_date", "expiry_date",
                     "file_storage_key"):
            assert want in cols, f"missing column: {want}"
        # Importable via top-level namespace.
        from app.models import (
            RequiredDocumentType, EmployeeDocument,
            EmployeeDocumentStatus,
        )
        _ = (RequiredDocumentType, EmployeeDocument,
             EmployeeDocumentStatus)
        return "2 tables + models exported"


@check("2. HTTP: create RDT as owner → row lands; duplicate → error")
def _():
    from app import create_app, db
    from app.models import RequiredDocumentType
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("EDOC2")
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(oid)
            sess["_fresh"] = True
            sess["active_company_id"] = cid
        r = client.post("/hr/documents/types", data={
            "name_ar": "الفيش الجنائي",
            "is_mandatory": "1",
            "has_expiry": "1",
            "default_validity_months": "6",
        })
        assert r.status_code in (200, 302), r.status_code
        row = RequiredDocumentType.query.filter_by(
            company_id=cid, name_ar="الفيش الجنائي").first()
        assert row is not None
        assert row.is_mandatory and row.has_expiry
        assert row.default_validity_months == 6
        # Duplicate name → same tenant → refused (unique-index).
        r2 = client.post("/hr/documents/types", data={
            "name_ar": "الفيش الجنائي"})
        # We flash + redirect — no second row.
        assert r2.status_code in (200, 302)
        cnt = RequiredDocumentType.query.filter_by(
            company_id=cid, name_ar="الفيش الجنائي").count()
        assert cnt == 1, f"duplicate slipped through: {cnt}"
        return "RDT created; duplicate rejected"


@check("3. missing_documents_report — active emp + no doc → "
        "appears; after submit → empty")
def _():
    from app import create_app, db
    from app.services.employee_documents import (
        submit_document, missing_documents_report,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("EDOC3")
        emp = _make_employee(cid, "علي")
        dt = _make_type(cid, name_ar="بطاقة الرقم القومي")
        rep = missing_documents_report(cid)
        assert len(rep) == 1
        assert rep[0]["employee"].id == emp.id
        assert rep[0]["missing"][0][0].id == dt.id
        assert rep[0]["missing"][0][1] == "لم تُقدَّم"
        # Now submit — no file.
        submit_document(emp, dt, created_by_id=oid)
        rep2 = missing_documents_report(cid)
        assert rep2 == [], f"still missing after submit: {rep2}"
        return "1 missing → 0 missing after submit"


@check("4. expiry: default_validity_months auto-computes; past "
        "date → 'منتهية الصلاحية'")
def _():
    from app import create_app, db
    from app.services.employee_documents import (
        submit_document, missing_documents_report,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("EDOC4")
        emp = _make_employee(cid, "منى")
        dt = _make_type(cid, name_ar="شهادة صحية",
                         has_expiry=True,
                         default_validity_months=6)
        row = submit_document(emp, dt, created_by_id=oid)
        assert row.expiry_date is not None
        # ~6 months = ~180 days from today.
        delta_days = (row.expiry_date - date.today()).days
        assert 170 <= delta_days <= 190, delta_days
        # Force expiry to yesterday.
        row.expiry_date = date.today() - timedelta(days=1)
        db.session.commit()
        rep = missing_documents_report(cid)
        assert len(rep) == 1
        assert rep[0]["missing"][0][1] == "منتهية الصلاحية"
        return f"auto-expiry {delta_days} days; forced past → expired"


@check("5. HTTP file upload + private download round-trip")
def _():
    from werkzeug.datastructures import FileStorage
    from app import create_app, db
    from app.models import EmployeeDocument
    from app.services.employee_documents import (
        _root, resolve_disk_path,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("EDOC5")
        emp = _make_employee(cid, "خالد")
        dt = _make_type(cid, name_ar="شهادة المؤهل")
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(oid)
            sess["_fresh"] = True
            sess["active_company_id"] = cid
        fake_pdf = (b"%PDF-1.4\n%fake pdf body for audit\n"
                    + b"0" * 200)
        r = client.post(
            f"/hr/documents/employees/{emp.id}/submit/{dt.id}",
            data={
                "file": (io.BytesIO(fake_pdf), "cert.pdf",
                          "application/pdf"),
                "notes": "test upload",
            },
            content_type="multipart/form-data",
        )
        assert r.status_code in (200, 302), r.status_code
        db.session.expire_all()
        doc = EmployeeDocument.query.filter_by(
            employee_id=emp.id, document_type_id=dt.id).first()
        assert doc is not None
        assert doc.file_storage_key, "file_storage_key not set"
        assert doc.file_size_bytes == len(fake_pdf), \
            f"size mismatch: {doc.file_size_bytes} vs {len(fake_pdf)}"
        assert doc.file_original_name == "cert.pdf"
        disk_path = resolve_disk_path(doc)
        assert disk_path.exists(), f"file missing: {disk_path}"
        # Download.
        r2 = client.get(
            f"/hr/documents/employees/{emp.id}/file/{doc.id}")
        assert r2.status_code == 200, r2.status_code
        assert r2.mimetype == "application/pdf"
        assert r2.data.startswith(b"%PDF"), r2.data[:20]
        return f"uploaded {doc.file_size_bytes} bytes; served OK"


@check("6. cross-tenant guard: 404 on both submit + file endpoints")
def _():
    from app import create_app, db
    app = create_app()
    with app.app_context():
        # Tenant A + its employee + type.
        email_a, cid_a, oid_a = _boot("EDOC6A")
        emp_a = _make_employee(cid_a, "علي أ")
        dt_a = _make_type(cid_a, name_ar="بطاقة")
        # Tenant B + its owner.
        email_b, cid_b, oid_b = _boot("EDOC6B")
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(oid_b)
            sess["_fresh"] = True
            sess["active_company_id"] = cid_b
        # Submit against tenant A's employee id.
        r = client.post(
            f"/hr/documents/employees/{emp_a.id}/submit/{dt_a.id}",
            data={})
        assert r.status_code == 404, \
            f"submit cross-tenant leaked: {r.status_code}"
        # A fake file-id lookup for tenant A → 404 too.
        r2 = client.get(
            f"/hr/documents/employees/{emp_a.id}/file/999999")
        assert r2.status_code == 404, r2.status_code
        return "cross-tenant → 404 on both endpoints"


@check("7. deactivate an RDT — drops from report + submission list, "
        "historical rows stay")
def _():
    from app import create_app, db
    from app.models import (
        RequiredDocumentType, EmployeeDocument,
    )
    from app.services.employee_documents import (
        submit_document, missing_documents_report,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("EDOC7")
        emp = _make_employee(cid, "سالم")
        dt1 = _make_type(cid, name_ar="بطاقة")
        dt2 = _make_type(cid, name_ar="شهادة")
        # Submit dt1 so it has a historical row.
        submit_document(emp, dt1, created_by_id=oid)
        assert len(missing_documents_report(cid)) == 1  # dt2 missing
        # Deactivate dt2 → report becomes empty.
        dt2.is_active = False
        db.session.commit()
        rep = missing_documents_report(cid)
        assert rep == [], f"deactivated type still in report: {rep}"
        # Historical dt1 row still there.
        hist = EmployeeDocument.query.filter_by(
            employee_id=emp.id, document_type_id=dt1.id).first()
        assert hist is not None
        # The active-type list drops dt2.
        active = RequiredDocumentType.query.filter_by(
            company_id=cid, is_active=True).all()
        assert {t.id for t in active} == {dt1.id}
        return "dt2 deactivated → gone from report; dt1 history kept"


@check("8. TERMINATED employee excluded from missing report")
def _():
    from app import create_app, db
    from app.models import EmployeeStatus
    from app.services.employee_documents import (
        missing_documents_report,
    )
    app = create_app()
    with app.app_context():
        email, cid, oid = _boot("EDOC8")
        # One active + one terminated, both missing the same paper.
        active = _make_employee(cid, "نشط")
        gone = _make_employee(cid, "مفصول",
                               status_val=EmployeeStatus.TERMINATED)
        _make_type(cid, name_ar="مستند")
        rep = missing_documents_report(cid)
        emp_ids = {r["employee"].id for r in rep}
        assert active.id in emp_ids
        assert gone.id not in emp_ids, \
            "terminated employee leaked into report"
        return "only active employee flagged"


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
