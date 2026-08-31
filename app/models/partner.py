from datetime import datetime
from app import db


class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    # MARSOUD-TKT-ADMIN-OWNER-COL (2026-08-31) — the contact person
    # representing this customer (e.g. "Hazem Amr" for the customer
    # company "Brand Builders"). Nullable so individuals-as-customers
    # can leave it blank. Shown as a clickable column on
    # /customers/ that links to the customer view page.
    contact_person = db.Column(db.String(150), nullable=True)
    email = db.Column(db.String(150))
    phone = db.Column(db.String(50))
    address = db.Column(db.Text)
    tax_number = db.Column(db.String(50))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # MARSOUD-COMM-01 — sales rep + commission rate per customer.
    # Both nullable: a customer without a sales_rep generates no
    # commission rows on payments.
    sales_rep_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    commission_rate = db.Column(db.Numeric(5, 2))   # % on pre-tax taxable share
    # MARSOUD-COA-REBUILD — every customer owns a sub-account under 1130
    # (Accounts Receivable). Created at customer-create time; invoicing
    # posts AR debits here instead of the parent header.
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)

    # foreign_keys pinned to company_id because Company also has
    # saas_customer_id → customers.id (MARSOUD-SAAS-BILLING-01),
    # which would otherwise make the join ambiguous.
    company = db.relationship("Company", foreign_keys=[company_id],
                              backref=db.backref("customers", lazy="dynamic"))
    sales_rep = db.relationship("User", foreign_keys=[sales_rep_id])
    account = db.relationship("Account", foreign_keys=[account_id])

    @property
    def balance(self):
        """MARSOUD-PARTY-OPENING-BALANCE-01 — subsidiary sub-account
        (under 1130) is now the single source of truth. Opening balances,
        refunds, credit notes, and receipts all end up in one number.
        Legacy customers without a sub-account fall back to the old
        sum-of-invoices calc to preserve pre-rebuild behaviour."""
        if self.account_id and self.account is not None:
            return float(self.account.balance or 0)
        return sum(inv.balance for inv in self.invoices
                    if inv.status.value not in ("CANCELLED", "REFUNDED"))


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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # MARSOUD-COA-REBUILD — vendor sub-account under 2110 (AP).
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)

    company = db.relationship("Company", backref=db.backref("vendors", lazy="dynamic"))
    account = db.relationship("Account", foreign_keys=[account_id])

    @property
    def balance(self):
        """MARSOUD-PARTY-OPENING-BALANCE-01 — new. Amount we owe the
        vendor, taken directly from the sub-account under 2110. Positive
        = we owe them; negative = we prepaid. Legacy vendors without a
        sub-account fall back to 0 (there's no cross-check available)."""
        if self.account_id and self.account is not None:
            return float(self.account.balance or 0)
        return 0.0
