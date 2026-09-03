"""MARSOUD-HR-EMPLOYEE-DOCS-01 (2026-09-03) — HR document tracking.

Public API:
  * `submit_document(employee, doc_type, ...)`  — upsert the row for
    (employee, doc_type) to SUBMITTED; optional file upload.
  * `missing_documents_report(company_id)`      — per-employee list
    of missing / expired papers (used by the /hr/documents/missing
    page).
  * `count_employees_with_missing_docs(company_id)` — cheap tile
    metric.
  * `resolve_disk_path(doc)` / `delete_file(doc)` — file-side
    helpers used by the download route.

Storage mirrors `app/services/user_files.py:44-155`: files live
under `app/private_uploads/employee_documents/<company>/<employee>/
<uuid>.<ext>` (private, NOT under /static/). The uuid handle is
stored in `EmployeeDocument.file_storage_key`; the Arabic original
filename stays in `file_original_name` for display.
"""
import mimetypes
import os
import uuid
from datetime import date, datetime
from pathlib import Path

from flask import current_app
from werkzeug.utils import secure_filename    # noqa: F401 — kept for future use

from app import db
from app.models import (
    Employee, EmployeeStatus,
    RequiredDocumentType, EmployeeDocument, EmployeeDocumentStatus,
)


class DocumentError(Exception):
    """Raised for user-facing upload / update failures."""


# HR documents are almost always PDF scans or camera photos of ID
# cards. Word/Excel would be misuse. Kept narrower than user_files.
_ALLOWED_EXTS = {"pdf", "png", "jpg", "jpeg", "webp", "heic"}
_MAX_BYTES = 10 * 1024 * 1024   # 10 MB per file


def _root() -> Path:
    """Base directory for private employee-document uploads. Created
    lazily so a fresh checkout doesn't need a manual mkdir step."""
    root = (Path(current_app.root_path)
            / "private_uploads" / "employee_documents")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _extract_ext(original_filename: str) -> str:
    if not original_filename or "." not in original_filename:
        return ""
    return original_filename.rsplit(".", 1)[-1].lower().strip()


def _save_file(company_id: int, employee_id: int, file_storage):
    """Persist a FileStorage under
    private_uploads/employee_documents/<co>/<emp>/<uuid>.<ext>.
    Returns (storage_key, original_name, size_bytes, mimetype).
    Raises DocumentError on validation failure."""
    if not file_storage or not file_storage.filename:
        raise DocumentError("لم يُرفع أي ملف")

    original = file_storage.filename
    ext = _extract_ext(original)
    if ext not in _ALLOWED_EXTS:
        raise DocumentError(
            "صيغة غير مدعومة. المسموح: PDF / JPG / PNG / WEBP / HEIC")

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size > _MAX_BYTES:
        raise DocumentError(
            f"الملف يتجاوز الحد الأقصى "
            f"({_MAX_BYTES // (1024*1024)} ميجا)")
    if size <= 0:
        raise DocumentError("الملف فارغ")

    dest_dir = _root() / str(company_id) / str(employee_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    key_name = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(str(dest_dir / key_name))

    # storage_key is company-scoped so resolve_disk_path can rebuild
    # the full path without carrying the employee id separately.
    storage_key = f"{company_id}/{employee_id}/{key_name}"
    mimetype = (mimetypes.guess_type(original)[0]
                or file_storage.mimetype or "application/octet-stream")
    return storage_key, original, size, mimetype


def resolve_disk_path(doc: EmployeeDocument) -> Path:
    """Absolute Path for a stored file. Never return this to the
    client — route code hands it to send_file."""
    if not doc.file_storage_key:
        raise DocumentError("لا يوجد ملف مرفوع")
    return _root() / doc.file_storage_key


def delete_file(doc: EmployeeDocument) -> None:
    """Remove ONLY the file blob (not the row). Idempotent — silent
    on missing disk entries."""
    if not doc.file_storage_key:
        return
    try:
        p = resolve_disk_path(doc)
        if p.exists():
            p.unlink()
    except (OSError, DocumentError):
        pass
    doc.file_storage_key = None
    doc.file_original_name = None
    doc.file_size_bytes = None
    doc.file_mimetype = None


def submit_document(employee, document_type, *,
                     submitted_date=None, file_storage=None,
                     notes=None, expiry_date=None,
                     created_by_id=None):
    """Upsert the (employee × document_type) row to
    status=SUBMITTED. Because of the unique-index, at most one row
    exists per tuple — a second call updates in place instead of
    creating a duplicate. When a file is uploaded and the row
    already carries an older blob, the old file is removed from
    disk (only-latest policy per ticket §3)."""
    if employee.company_id != document_type.company_id:
        raise DocumentError(
            "نوع المستند لا يخص شركة الموظف")

    row = EmployeeDocument.query.filter_by(
        employee_id=employee.id,
        document_type_id=document_type.id).first()
    if not row:
        row = EmployeeDocument(
            company_id=employee.company_id,
            employee_id=employee.id,
            document_type_id=document_type.id,
        )
        db.session.add(row)

    row.status = EmployeeDocumentStatus.SUBMITTED
    row.submitted_date = submitted_date or date.today()
    row.notes = (notes or "").strip() or None
    row.created_by_id = created_by_id

    # Explicit expiry_date wins; otherwise auto-compute from the
    # type's validity window when the paper is renewable.
    if expiry_date:
        row.expiry_date = expiry_date
    elif document_type.has_expiry and document_type.default_validity_months:
        # Approx-month arithmetic — 30 days per unit is intentional
        # for renewal reminders (bureaucracy expiries are lax).
        from datetime import timedelta
        row.expiry_date = (row.submitted_date + timedelta(
            days=30 * document_type.default_validity_months))
    else:
        row.expiry_date = None

    # File upload — delete any existing blob first so the disk
    # doesn't accumulate stale copies.
    if file_storage and file_storage.filename:
        if row.file_storage_key:
            delete_file(row)
        storage_key, original, size, mimetype = _save_file(
            employee.company_id, employee.id, file_storage)
        row.file_storage_key = storage_key
        row.file_original_name = original
        row.file_size_bytes = size
        row.file_mimetype = mimetype

    db.session.commit()
    return row


def missing_documents_report(company_id):
    """Return every ACTIVE employee's missing / expired mandatory
    papers. Terminated employees are excluded per ticket §9.5.

    Shape:
        [{"employee": Employee,
          "missing":  [(RequiredDocumentType, reason_ar), ...]}]

    reason_ar is one of "لم تُقدَّم" / "منتهية الصلاحية".
    """
    today = date.today()
    types = (RequiredDocumentType.query
             .filter_by(company_id=company_id,
                         is_active=True, is_mandatory=True)
             .order_by(RequiredDocumentType.name_ar).all())
    if not types:
        return []
    employees = (Employee.query
                  .filter_by(company_id=company_id,
                              status=EmployeeStatus.ACTIVE)
                  .order_by(Employee.name).all())

    result = []
    for emp in employees:
        # One query per employee (dynamic backref) — small N in
        # practice; if a tenant has 500+ employees we swap this
        # for a single grouped query later.
        existing = {d.document_type_id: d for d in emp.documents}
        missing = []
        for dt in types:
            rec = existing.get(dt.id)
            if rec is None or rec.status == EmployeeDocumentStatus.MISSING:
                missing.append((dt, "لم تُقدَّم"))
            elif (dt.has_expiry and rec.expiry_date
                    and rec.expiry_date < today):
                missing.append((dt, "منتهية الصلاحية"))
        if missing:
            result.append({"employee": emp, "missing": missing})
    return result


def count_employees_with_missing_docs(company_id):
    """Cheap dashboard metric — 0 when the tenant has no required
    types configured (avoids alarming the owner about a feature
    they haven't turned on)."""
    return len(missing_documents_report(company_id))


def documents_for_employee(employee):
    """Return a dict {document_type_id: EmployeeDocument} the
    profile template can index directly."""
    return {d.document_type_id: d for d in employee.documents}
