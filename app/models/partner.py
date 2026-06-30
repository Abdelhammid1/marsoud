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
    # MARSOUD-COMM-01 — sales rep + commission rate per customer.
    # Both nullable: a customer without a sales_rep generates no
    # commission rows on payments.
    sales_rep_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    commission_rate = db.Column(db.Numeric(5, 2))   # % on pre-tax taxable share
    # MARSOUD-COA-REBUILD — every customer owns a sub-account under 1130
    # (Accounts Receivable). Created at customer-create time; invoicing
    # posts AR debits here instead of the parent header.
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)

    company = db.relationship("Company", backref=db.backref("customers", lazy="dynamic"))
    sales_rep = db.relationship("User", foreign_keys=[sales_rep_id])
    account = db.relationship("Account", foreign_keys=[account_id])

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
    # MARSOUD-COA-REBUILD — vendor sub-account under 2110 (AP).
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)

    company = db.relationship("Company", backref=db.backref("vendors", lazy="dynamic"))
    account = db.relationship("Account", foreign_keys=[account_id])
