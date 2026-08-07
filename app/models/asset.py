import enum
from datetime import datetime, date
from app import db


class DisposalReason(enum.Enum):
    """MARSOUD-ASSET-DISPOSAL-01 (2026-08-07) — why an asset left the
    books. Enum (not free text) so the fixed-assets report + the
    accountant agent can filter/count by reason without free-text
    parsing. `disposal_note` handles the extra detail beside it."""
    SOLD        = "SOLD"          # بيع بمقابل
    LOST        = "LOST"          # فقد / سرقة
    DAMAGED     = "DAMAGED"       # تلف
    END_OF_LIFE = "END_OF_LIFE"   # انتهى عمره الإنتاجي
    OTHER       = "OTHER"         # أي سبب آخر — راجع disposal_note

    @property
    def label_ar(self):
        return {
            "SOLD":        "بيع",
            "LOST":        "فقد",
            "DAMAGED":     "تلف",
            "END_OF_LIFE": "انتهاء العمر الإنتاجي",
            "OTHER":       "أخرى",
        }[self.value]


class FixedAsset(db.Model):
    __tablename__ = "fixed_assets"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    purchase_date = db.Column(db.Date, default=date.today, nullable=False)
    cost = db.Column(db.Numeric(15, 4), nullable=False)
    salvage_value = db.Column(db.Numeric(15, 4), default=0)
    useful_life_years = db.Column(db.Integer, nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"))
    vendor_id = db.Column(db.Integer, db.ForeignKey("vendors.id"))
    source_bill_id = db.Column(db.Integer, db.ForeignKey("vendor_bills.id"))
    accumulated_depreciation = db.Column(db.Numeric(15, 4), default=0)
    is_disposed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # MARSOUD-ASSET-DISPOSAL-01 (2026-08-07) — the closure fields.
    # All five populated in one atomic write by
    # `app/services/assets.py::dispose_asset` alongside a balanced
    # disposal journal. Never edited by hand.
    disposal_date = db.Column(db.Date, nullable=True)
    disposal_reason = db.Column(db.Enum(DisposalReason), nullable=True)
    disposal_note = db.Column(db.Text, nullable=True)
    disposal_proceeds = db.Column(db.Numeric(15, 2), default=0)
    disposal_journal_entry_id = db.Column(
        db.Integer, db.ForeignKey("journal_entries.id"), nullable=True)
    disposed_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True)

    company = db.relationship("Company", backref=db.backref("assets", lazy="dynamic"))
    account = db.relationship("Account")
    vendor = db.relationship("Vendor", backref="fixed_assets")
    disposal_entry = db.relationship(
        "JournalEntry", foreign_keys=[disposal_journal_entry_id])
    disposed_by = db.relationship("User", foreign_keys=[disposed_by_id])

    @property
    def annual_depreciation(self):
        return (float(self.cost) - float(self.salvage_value)) / max(self.useful_life_years, 1)

    @property
    def monthly_depreciation(self):
        return self.annual_depreciation / 12.0

    @property
    def net_book_value(self):
        return float(self.cost) - float(self.accumulated_depreciation or 0)

    def depreciated_for_period(self, year, month):
        """True if depreciation has already been posted for this asset/period."""
        return DepreciationEntry.query.filter_by(
            asset_id=self.id, period_year=year, period_month=month
        ).first() is not None


class DepreciationEntry(db.Model):
    """One row per asset per month — guarantees we never depreciate twice
    in the same period (enforced by unique constraint)."""
    __tablename__ = "depreciation_entries"
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("fixed_assets.id"), nullable=False)
    period_year = db.Column(db.Integer, nullable=False)
    period_month = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Numeric(15, 4), nullable=False)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("journal_entries.id"))
    book_value_after = db.Column(db.Numeric(15, 4))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    asset = db.relationship("FixedAsset", backref="depreciation_history")

    __table_args__ = (
        db.UniqueConstraint("asset_id", "period_year", "period_month",
                            name="uq_depreciation_asset_period"),
    )
