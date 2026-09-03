"""MARSOUD-HR-EMPLOYEE-DOCS-01 (2026-09-03) — per-employee document
tracking.

Two models:

  RequiredDocumentType  — per-tenant catalogue of paper types HR
                          needs on file. Each tenant defines its
                          own list (same philosophy as CostCenter).
  EmployeeDocument      — one row per (employee, document_type)
                          tuple recording whether the paper is
                          submitted, when, and where the optional
                          uploaded scan lives on disk.

Files themselves live under
`app/private_uploads/employee_documents/<company>/<employee>/<uuid>.<ext>`
(the user_files.py pattern — private, uuid-keyed, auth-gated on
read). We keep the Arabic original filename for display and store
the opaque UUID as `file_storage_key`.
"""
import enum
from datetime import datetime
from app import db


class EmployeeDocumentStatus(enum.Enum):
    MISSING = "MISSING"       # explicitly recorded as missing (rare;
                              # absence of any row implies missing too)
    SUBMITTED = "SUBMITTED"   # handed in — file optional
    EXPIRED = "EXPIRED"       # was submitted, its expiry_date passed

    @property
    def label_ar(self):
        return {
            "MISSING":   "لم تُقدَّم",
            "SUBMITTED": "مقدّمة",
            "EXPIRED":   "منتهية الصلاحية",
        }.get(self.value, self.value)


class RequiredDocumentType(db.Model):
    """A paper the tenant expects every employee to hand in. The
    LIST of types is per-tenant — the security-guarding client
    tracks 'الفيش والتشبيه' + 'شهادة صحية'; a school tracks
    'مؤهل دراسي' + 'شهادة خبرة'. Shipping a hard-coded catalogue
    would ignore that.
    """
    __tablename__ = "required_document_types"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                           db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    name_ar = db.Column(db.String(150), nullable=False)
    # is_mandatory=False types don't appear in the missing report
    # but can still be tracked per employee (e.g. "شهادة تدريب"
    # nice-to-have).
    is_mandatory = db.Column(db.Boolean, default=True, nullable=False)
    # For papers that renew (police clearance, health certificate) —
    # when True the report also flags rows whose expiry_date < today.
    has_expiry = db.Column(db.Boolean, default=False, nullable=False)
    # Auto-computed expiry offset. NULL means the user picks a date
    # manually on each submit.
    default_validity_months = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime,
                            default=datetime.utcnow, nullable=False)

    company = db.relationship("Company")

    __table_args__ = (
        db.UniqueConstraint("company_id", "name_ar",
                             name="uq_doc_type_company_name"),
    )

    def __repr__(self):
        return f"<RequiredDocumentType {self.id} {self.name_ar!r}>"


class EmployeeDocument(db.Model):
    """One row per (employee × doc_type). Absence of a row for a
    given (employee, active mandatory type) means the paper is
    missing — we don't backfill placeholder rows when a new type
    is added, so the report treats the join naturally.
    """
    __tablename__ = "employee_documents"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                           db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    employee_id = db.Column(db.Integer,
                            db.ForeignKey("employees.id",
                                           ondelete="CASCADE"),
                            nullable=False, index=True)
    document_type_id = db.Column(
        db.Integer,
        db.ForeignKey("required_document_types.id",
                      ondelete="RESTRICT"),
        nullable=False, index=True)

    status = db.Column(db.Enum(EmployeeDocumentStatus),
                       default=EmployeeDocumentStatus.SUBMITTED,
                       nullable=False)
    submitted_date = db.Column(db.Date, nullable=True)
    expiry_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    # File-storage handles — all nullable so "recorded but not
    # uploaded" is a valid state (some tenants only care about the
    # paper checkbox, not scanning). uuid-keyed like user_files.py.
    file_storage_key = db.Column(db.String(200), nullable=True)
    file_original_name = db.Column(db.String(255), nullable=True)
    file_size_bytes = db.Column(db.Integer, nullable=True)
    file_mimetype = db.Column(db.String(120), nullable=True)

    created_by_id = db.Column(db.Integer,
                              db.ForeignKey("users.id"),
                              nullable=True)
    created_at = db.Column(db.DateTime,
                            default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    employee = db.relationship(
        "Employee",
        backref=db.backref("documents",
                            cascade="all, delete-orphan",
                            lazy="dynamic"))
    document_type = db.relationship("RequiredDocumentType")

    __table_args__ = (
        db.UniqueConstraint("employee_id", "document_type_id",
                             name="uq_employee_document_type"),
    )

    @property
    def has_file(self):
        return bool(self.file_storage_key)

    def __repr__(self):
        return (f"<EmployeeDocument {self.id} emp={self.employee_id} "
                f"type={self.document_type_id} {self.status.value}>")
