#!/usr/bin/env python3
"""MARSOUD-TKT-PDFS-04-PAYROLL (Abdelhamid 2026-08-29) — payroll PDFs
must render via the shared WeasyPrint shell.

Continues MARSOUD-TKT-PDFS-01/02/03 ratchet. Ticket 4 migrates
`export_payroll_run_pdf` + the PDF branch of `export_payroll_summary`
to WeasyPrint templates extending pdfs/_shell.html + macros.

Note: pdfs/payslip.html (individual employee slip) was already
WeasyPrint on the shared design language before this session — not in
scope here.

Checks:
  1. Both templates extend _shell.html + import _report_macros.html.
  2. payroll_run.html carries 7 required Arabic columns + outcome block.
  3. payroll_summary.html carries 9 required Arabic columns + outcome block.
  4. export_payroll_run_pdf: WeasyPrint-first + legacy fallback.
  5. export_payroll_summary PDF branch: WeasyPrint-first + legacy fallback.
  6. Excel branch of export_payroll_summary preserved.
  7. End-to-end: bootstrap a company + employee + a run, render both
     PDFs via the service functions, assert %PDF + Amiri embedded.
"""
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "0")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TPL = ROOT / "app" / "templates"


CHECKS = []


def check(label):
    def deco(fn):
        CHECKS.append((label, fn))
        return fn
    return deco


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


@check("1. both templates extend _shell.html + import _report_macros.html")
def _():
    for path in ("payroll_run", "payroll_summary"):
        src = _strip_comments(_read(f"app/templates/pdfs/{path}.html"))
        assert 'extends "pdfs/_shell.html"' in src, \
            f"{path}.html does not extend pdfs/_shell.html"
        assert 'import "pdfs/_report_macros.html"' in src, \
            f"{path}.html does not import _report_macros.html"
    return "both payroll templates wired to shell + macros"


@check("2. payroll_run.html: 7 required columns + outcome block")
def _():
    src = _strip_comments(_read("app/templates/pdfs/payroll_run.html"))
    assert "كشف رواتب الشهر" in src
    for col in ("الموظف", "أيام العمل", "الأساسي", "البدلات",
                "بونص+أوفرتايم", "الخصومات", "الصافي"):
        assert col in src, f"payroll_run.html missing column: {col!r}"
    assert "rpt.outcome_block" in src, \
        "payroll_run.html does not emit total outcome_block"
    return "payroll_run template complete"


@check("3. payroll_summary.html: 9 required columns + outcome block")
def _():
    src = _strip_comments(_read("app/templates/pdfs/payroll_summary.html"))
    assert "ملخص الرواتب" in src
    for col in ("الفترة", "الكشف", "الموظف", "الأساسي", "البدلات",
                "أوفرتايم", "بونص", "خصومات", "الصافي"):
        assert col in src, f"payroll_summary.html missing column: {col!r}"
    assert "rpt.outcome_block" in src, \
        "payroll_summary.html does not emit total outcome_block"
    return "payroll_summary template complete"


@check("4. export_payroll_run_pdf: WeasyPrint-first + legacy fallback")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    body = _extract_body(src, "export_payroll_run_pdf")
    assert "_weasyprint_render(" in body, \
        "export_payroll_run_pdf no longer calls _weasyprint_render"
    assert '"pdfs/payroll_run.html"' in body
    assert "_export_payroll_run_pdf_legacy" in body
    assert re.search(r"^def _export_payroll_run_pdf_legacy\(", src, re.MULTILINE)
    return "payroll_run: WeasyPrint primary + ReportLab fallback"


@check("5. export_payroll_summary PDF branch: WeasyPrint + legacy fallback")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    body = _extract_body(src, "export_payroll_summary")
    assert 'fmt == "pdf"' in body, "lost fmt branching"
    assert "_weasyprint_render(" in body
    assert '"pdfs/payroll_summary.html"' in body
    assert "_export_payroll_summary_pdf_legacy" in body
    assert re.search(r"^def _export_payroll_summary_pdf_legacy\(", src, re.MULTILINE)
    return "payroll_summary PDF: WeasyPrint primary + ReportLab fallback"


@check("6. Excel branch of export_payroll_summary preserved")
def _():
    src = _strip_comments(_read("app/services/export.py"))
    body = _extract_body(src, "export_payroll_summary")
    assert "_list_excel(" in body, \
        "export_payroll_summary Excel branch was accidentally removed"
    return "Excel branch untouched"


@check("7. end-to-end: render both payroll PDFs + Amiri embedded")
def _():
    from datetime import datetime, date
    from decimal import Decimal
    from sqlalchemy import text, inspect
    from app import create_app, db

    app = create_app()
    with app.app_context():
        from app.models import Company, User, UserStatus, Plan
        from app.models.user import user_companies
        from app.models.payroll import Employee, EmployeeStatus, ContractType
        from app.services.seed_coa import seed_default_coa
        from app.services.payroll import run_payroll
        from app.services.export import (
            export_payroll_run_pdf, export_payroll_summary,
        )
        from werkzeug.security import generate_password_hash
        insp = inspect(db.engine)

        # Clean prior
        cids = [r[0] for r in db.session.execute(text(
            "SELECT id FROM companies WHERE name LIKE '__PAYROLL_AUDIT__%'"))]
        for cid in cids:
            for t in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": cid})
            db.session.execute(text("DELETE FROM user_companies WHERE company_id = :c"), {"c": cid})
            db.session.execute(text("DELETE FROM companies WHERE id = :c"), {"c": cid})
        db.session.execute(text(
            "DELETE FROM users WHERE email LIKE 'payroll-audit-%@x.test'"))
        # Nuke orphan payroll_lines. SQLite reuses payroll_run ids after
        # deletion, and a fresh run's id can collide with old lines from
        # a previous test whose Employee was deleted but whose lines
        # remained (payroll_lines has no company_id column, so the
        # per-company cleanup above misses them). Two sweeps:
        #   (a) lines whose run_id doesn't exist any more
        #   (b) lines whose employee_id doesn't exist any more
        # Same class of trick Ticket 1's audit uses for journal_lines.
        db.session.execute(text(
            "DELETE FROM payroll_lines "
            "WHERE run_id NOT IN (SELECT id FROM payroll_runs)"))
        db.session.execute(text(
            "DELETE FROM payroll_lines "
            "WHERE employee_id NOT IN (SELECT id FROM employees)"))
        db.session.commit()

        # Bootstrap: company + plan with hr module + owner + one employee
        plan = None
        for candidate in Plan.query.filter_by(is_active=True).all():
            if "hr" in (candidate.modules or []):
                plan = candidate
                break
        if plan is None:
            plan = Plan.query.filter_by(is_active=True).first()

        c = Company(name="__PAYROLL_AUDIT__منصتي", base_currency="EGP",
                    subdomain="payroll-audit-1",
                    subscription_started_at=datetime.utcnow(),
                    subscription_expires_at=datetime(2999, 1, 1),
                    intended_plan_id=plan.id if plan else None,
                    plan_id=plan.id if plan else None)
        db.session.add(c); db.session.commit()
        seed_default_coa(c.id)
        db.session.commit()

        # Owner (needed by run_payroll's audit logging)
        u = User(email="payroll-audit-1@x.test",
                 password_hash=generate_password_hash("TestPass123!", method="pbkdf2:sha256"),
                 full_name="payroll-audit", is_active=True,
                 status=UserStatus.ACTIVE.value,
                 email_verified_at=datetime.utcnow(),
                 terms_version="TEST")
        db.session.add(u); db.session.flush()
        db.session.execute(user_companies.insert().values(
            user_id=u.id, company_id=c.id, role="owner"))
        db.session.commit()

        emp = Employee(company_id=c.id, name="موظف اختبار",
            employee_number="EMP-P4-1",
            basic_salary=Decimal("5000.00"),
            start_date=date(2026, 1, 1),
            status=EmployeeStatus.ACTIVE)
        db.session.add(emp); db.session.commit()

        today = date.today()
        run = run_payroll(
            company_id=c.id, year=today.year, month=today.month,
            line_inputs={emp.id: {"amount_paid": 0}},
            send_emails=False)
        db.session.commit()
        # Warm relationships so template access (line.employee.name)
        # doesn't hit stale-session NoneType — same trick Ticket 1's
        # end-to-end check uses.
        from app.models.payroll import PayrollRun
        db.session.expire_all()
        run = db.session.get(PayrollRun, run.id)
        for line in run.lines:
            _ = line.employee.name

        try:
            # 1) Single-run PDF
            buf = export_payroll_run_pdf(run)
            data = buf.read()
            assert data.startswith(b"%PDF"), "payroll_run: not a PDF"
            assert b"Amiri" in data, "payroll_run: Amiri font not embedded"
            r1 = f"payroll_run {len(data)}B"

            # 2) Cross-period summary PDF
            out = export_payroll_summary(
                c, fmt="pdf", year=today.year, month=today.month)
            buf, filename, mime = out
            data = buf.read()
            assert data.startswith(b"%PDF"), "payroll_summary: not a PDF"
            assert b"Amiri" in data, "payroll_summary: Amiri font not embedded"
            r2 = f"payroll_summary {len(data)}B"
        finally:
            for t in reversed(db.metadata.sorted_tables):
                cols = {col["name"] for col in insp.get_columns(t.name)}
                if "company_id" in cols:
                    db.session.execute(text(f"DELETE FROM {t.name} WHERE company_id = :c"), {"c": c.id})
            db.session.execute(text("DELETE FROM user_companies WHERE company_id = :c"), {"c": c.id})
            db.session.execute(text("DELETE FROM companies WHERE id = :c"), {"c": c.id})
            db.session.execute(text("DELETE FROM users WHERE id = :u"), {"u": u.id})
            db.session.commit()

        return f"both PDFs valid + Amiri embedded: {r1}, {r2}"


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
