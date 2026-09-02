"""MARSOUD-COST-CENTERS-01 (2026-09-02) — مراكز التكلفة.

A flat accounting-side dimension that classifies journal lines
(who / where / why is this expense?). Deliberately separate from
`Department` (HR org chart, has `custody_account_id` for cash
custody but not touched by JE build) and from `Project` (CRM /
task management, no financial columns). Manual linking is
supported via `linked_department_id`, but there is NO automatic
sync — a Department is not implicitly a CostCenter.

Attached at the `JournalLine` grain via one new nullable column
so every existing JE stays valid (NULL = "unclassified").
"""
from datetime import datetime
from app import db


class CostCenter(db.Model):
    __tablename__ = "cost_centers"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                            db.ForeignKey("companies.id"),
                            nullable=False, index=True)
    code = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    name_ar = db.Column(db.String(120))
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Optional link to a Department — for the picker only, no
    # automatic derivation of the cost center from the department.
    linked_department_id = db.Column(
        db.Integer, db.ForeignKey("departments.id"), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)
    # Soft delete — allowed only when no JournalLine references it.
    deleted_at = db.Column(db.DateTime, nullable=True)

    company = db.relationship(
        "Company", backref=db.backref("cost_centers", lazy="dynamic"))
    linked_department = db.relationship(
        "Department", foreign_keys=[linked_department_id])

    __table_args__ = (
        db.UniqueConstraint("company_id", "code",
                             name="uq_cost_center_company_code"),
    )

    def __repr__(self):
        return f"<CostCenter {self.code} (co={self.company_id})>"
