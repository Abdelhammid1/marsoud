from datetime import datetime
from app import db


class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150))
    phone = db.Column(db.String(50))
    address = db.Column(db.Text)
    tax_number = db.Column(db.String(50))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    company = db.relationship("Company", backref=db.backref("customers", lazy="dynamic"))

    @property
    def balance(self):
        return sum(inv.balance for inv in self.invoices if inv.status.value not in ("CANCELLED", "REFUNDED"))


class Vendor(db.Model):
    __tablename__ = "vendors"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150))
    phone = db.Column(db.String(50))
    address = db.Column(db.Text)
    bank_account = db.Column(db.String(100))
    tax_number = db.Column(db.String(50))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    company = db.relationship("Company", backref=db.backref("vendors", lazy="dynamic"))
