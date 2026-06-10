from datetime import datetime
from app import db


class Department(db.Model):
    __tablename__ = "departments"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    manager_employee_id = db.Column(db.Integer, db.ForeignKey("employees.id"), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)

    company = db.relationship("Company", backref=db.backref("departments", lazy="dynamic"))
    manager = db.relationship("Employee", foreign_keys=[manager_employee_id], post_update=True)

    __table_args__ = (
        db.UniqueConstraint("company_id", "name", name="uq_department_company_name"),
    )

    def __repr__(self):
        return f"<Department {self.name} (co={self.company_id})>"
