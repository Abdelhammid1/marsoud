"""MARSOUD-HR-EMPLOYEE-DOCS-01 (2026-09-03) — HR document tracking
routes.

Blueprint `employee_documents` mounted at `/hr/documents`. Handles:

  * The per-tenant catalogue of required document types
  * Per-employee submission (with optional file upload)
  * Company-wide "who is missing what" report
  * Private-file download (auth-gated, NEVER served from /static/)

Same tenancy discipline as `hr_decisions`: cross-tenant lookups
return 404, never 403 — "hidden vs forbidden".
"""
from flask import (
    Blueprint, render_template, redirect, url_for, request, g, flash,
    abort, send_file,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    Employee, RequiredDocumentType, EmployeeDocument,
    EmployeeDocumentStatus,
)
from app.services.permissions import require_permission, has_permission
from app.services.employee_documents import (
    DocumentError, submit_document, missing_documents_report,
    resolve_disk_path, documents_for_employee,
)


bp = Blueprint("employee_documents", __name__)


def _load_employee_or_404(employee_id):
    emp = db.session.get(Employee, int(employee_id))
    if not emp or emp.company_id != g.active_company.id:
        abort(404)
    return emp


def _load_type_or_404(type_id):
    dt = db.session.get(RequiredDocumentType, int(type_id))
    if not dt or dt.company_id != g.active_company.id:
        abort(404)
    return dt


# ═══════════════════════════════════════════════════════════════════
# 1. Types catalogue — owner/admin/hr_manager
# ═══════════════════════════════════════════════════════════════════
@bp.route("/types", methods=["GET"])
@login_required
@require_permission("document_types.manage")
def types_index():
    cid = g.active_company.id
    types = (RequiredDocumentType.query
             .filter_by(company_id=cid)
             .order_by(RequiredDocumentType.is_active.desc(),
                        RequiredDocumentType.name_ar).all())
    return render_template("employee_documents/types_index.html",
                            types=types)


@bp.route("/types", methods=["POST"])
@login_required
@require_permission("document_types.manage")
def types_create():
    cid = g.active_company.id
    name_ar = (request.form.get("name_ar") or "").strip()
    if not name_ar:
        flash("اسم نوع المستند مطلوب", "error")
        return redirect(url_for("employee_documents.types_index"))
    # UniqueConstraint enforcement — surface a friendly Arabic
    # message rather than an IntegrityError.
    dupe = RequiredDocumentType.query.filter_by(
        company_id=cid, name_ar=name_ar).first()
    if dupe:
        flash("نوع مستند بنفس الاسم موجود بالفعل", "error")
        return redirect(url_for("employee_documents.types_index"))
    is_mandatory = request.form.get("is_mandatory") == "1"
    has_expiry = request.form.get("has_expiry") == "1"
    raw_months = (request.form.get("default_validity_months")
                  or "").strip()
    validity_months = None
    if has_expiry and raw_months:
        try:
            validity_months = int(raw_months)
            if validity_months <= 0:
                validity_months = None
        except (TypeError, ValueError):
            validity_months = None
    row = RequiredDocumentType(
        company_id=cid, name_ar=name_ar,
        is_mandatory=is_mandatory, has_expiry=has_expiry,
        default_validity_months=validity_months,
        is_active=True,
    )
    db.session.add(row); db.session.commit()
    flash(f"تم إضافة نوع مستند: {name_ar}", "success")
    return redirect(url_for("employee_documents.types_index"))


@bp.route("/types/<int:type_id>/toggle", methods=["POST"])
@login_required
@require_permission("document_types.manage")
def types_toggle(type_id):
    dt = _load_type_or_404(type_id)
    dt.is_active = not dt.is_active
    db.session.commit()
    flash(
        f"تم {'تفعيل' if dt.is_active else 'تعطيل'} النوع: {dt.name_ar}",
        "success")
    return redirect(url_for("employee_documents.types_index"))


# ═══════════════════════════════════════════════════════════════════
# 2. Per-employee submission
# ═══════════════════════════════════════════════════════════════════
@bp.route("/employees/<int:employee_id>/submit/<int:type_id>",
          methods=["POST"])
@login_required
@require_permission("employee_documents.manage")
def submit(employee_id, type_id):
    emp = _load_employee_or_404(employee_id)
    dt = _load_type_or_404(type_id)
    file_storage = request.files.get("file") or None
    notes = (request.form.get("notes") or "").strip() or None
    raw_submitted = (request.form.get("submitted_date") or "").strip()
    raw_expiry = (request.form.get("expiry_date") or "").strip()

    from datetime import datetime as _dt
    submitted_date = None
    if raw_submitted:
        try:
            submitted_date = _dt.strptime(
                raw_submitted, "%Y-%m-%d").date()
        except ValueError:
            flash("تاريخ التقديم غير صالح", "error")
            return redirect(url_for(
                "payroll.employee_profile", employee_id=emp.id))
    expiry_date = None
    if raw_expiry:
        try:
            expiry_date = _dt.strptime(
                raw_expiry, "%Y-%m-%d").date()
        except ValueError:
            flash("تاريخ الانتهاء غير صالح", "error")
            return redirect(url_for(
                "payroll.employee_profile", employee_id=emp.id))

    try:
        submit_document(
            emp, dt,
            submitted_date=submitted_date,
            expiry_date=expiry_date,
            file_storage=file_storage,
            notes=notes,
            created_by_id=current_user.id,
        )
        flash(f"تم تسجيل مستند «{dt.name_ar}»", "success")
    except DocumentError as e:
        flash(str(e), "error")
    return redirect(url_for(
        "payroll.employee_profile", employee_id=emp.id))


# ═══════════════════════════════════════════════════════════════════
# 3. Private file download — auth-gated
# ═══════════════════════════════════════════════════════════════════
@bp.route("/employees/<int:employee_id>/file/<int:doc_id>",
          methods=["GET"])
@login_required
def file(employee_id, doc_id):
    # Composite permission: manage OR just-view (a viewer role
    # legitimately needs to open a scanned ID card for reference).
    if not (has_permission("employee_documents.manage")
            or has_permission("employees.view")):
        abort(403)
    emp = _load_employee_or_404(employee_id)
    doc = db.session.get(EmployeeDocument, int(doc_id))
    if (not doc or doc.employee_id != emp.id
            or doc.company_id != g.active_company.id):
        abort(404)
    if not doc.file_storage_key:
        abort(404)
    disk = resolve_disk_path(doc)
    if not disk.exists():
        abort(404)
    # Filename hint uses the ORIGINAL Arabic name from the DB so
    # the browser saves it recognisably — send_file handles the
    # RFC 5987 encoding.
    return send_file(
        str(disk),
        mimetype=doc.file_mimetype or "application/octet-stream",
        as_attachment=False,
        download_name=doc.file_original_name or f"document-{doc.id}",
    )


# ═══════════════════════════════════════════════════════════════════
# 4. Missing-docs report — company-wide
# ═══════════════════════════════════════════════════════════════════
@bp.route("/missing", methods=["GET"])
@login_required
def missing_report():
    if not (has_permission("employee_documents.manage")
            or has_permission("employees.view")):
        abort(403)
    cid = g.active_company.id
    report = missing_documents_report(cid)
    total_types = RequiredDocumentType.query.filter_by(
        company_id=cid, is_active=True, is_mandatory=True).count()
    return render_template(
        "employee_documents/missing_report.html",
        report=report, total_types=total_types,
    )
