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

    # MARSOUD-TKT-ADMIN-OWNER-COL — resolved contact person for the
    # /customers/ list, with fallback ladder so the column doesn't
    # look empty across the board on day 1. Order:
    #   1. Manual `contact_person` (owner input always wins)
    #   2. First active portal user linked to this customer via
    #      User.linked_customer_id — a real person representing the
    #      customer (they have a portal login)
    #   3. If this Customer is the saas_customer_id of a Marsoud
    #      tenant Company (e.g. Manasety views their /customers/
    #      and sees a paying tenant), that tenant's owner
    #   4. None (template renders '—')
    #
    # Returns (name, source) so the template can badge the origin
    # ('portal (بديل)' / 'مالك التنانت (بديل)') and stay honest
    # about what's manual vs. auto-derived.
    @property
    def resolved_contact_person(self):
        # 1. Manual override
        if self.contact_person:
            return (self.contact_person, "manual")
        # 2. Portal user linked to this customer
        from app.models import User
        pu = (User.query
              .filter_by(linked_customer_id=self.id, is_active=True)
              .order_by(User.id.asc()).first())
        if pu:
            return (pu.full_name or pu.email, "portal")
        # 3. Marsoud tenant whose saas_customer_id points here → owner
        from app.models import Company
        from app.models.user import user_companies
        from app import db as _db
        tenant = Company.query.filter_by(saas_customer_id=self.id).first()
        if tenant:
            def _pick(role=None):
                q = (_db.session.query(User)
                     .join(user_companies, user_companies.c.user_id == User.id)
                     .filter(user_companies.c.company_id == tenant.id))
                if role is not None:
                    q = q.filter(user_companies.c.role == role)
                return q.order_by(User.id.asc()).first()
            u = _pick("owner") or _pick("admin") or _pick(None)
            if u:
                return (u.full_name or u.email, "saas_owner")
        return (None, None)


# ── MARSOUD-TKT-CUSTOMER-COMMENTS-NOTES (2026-08-31) ──────────────
# Two internal-only surfaces on the tenant customer detail page.
# `CustomerComment` is thread-style discussion between team members
# (mirrors TaskComment's shape). `CustomerNote` is a plain log — one
# free-text entry with timestamp + author, no threading. Neither is
# ever surfaced on the customer portal.
class CustomerComment(db.Model):
    __tablename__ = "customer_comments"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(
        db.Integer,
        db.ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)

    customer = db.relationship(
        "Customer", foreign_keys=[customer_id],
        backref=db.backref("comments",
                           order_by="CustomerComment.created_at.asc()",
                           cascade="all, delete-orphan"))
    user = db.relationship("User", foreign_keys=[user_id])


class CustomerNote(db.Model):
    __tablename__ = "customer_notes"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(
        db.Integer,
        db.ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)

    customer = db.relationship(
        "Customer", foreign_keys=[customer_id],
        backref=db.backref("notes",
                           order_by="CustomerNote.created_at.desc()",
                           cascade="all, delete-orphan"))
    user = db.relationship("User", foreign_keys=[user_id])


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
