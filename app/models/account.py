import enum
from app import db


class AccountType(enum.Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"


class NormalSide(enum.Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


NORMAL_SIDE_FOR_TYPE = {
    AccountType.ASSET: NormalSide.DEBIT,
    AccountType.EXPENSE: NormalSide.DEBIT,
    AccountType.LIABILITY: NormalSide.CREDIT,
    AccountType.EQUITY: NormalSide.CREDIT,
    AccountType.REVENUE: NormalSide.CREDIT,
}


class Account(db.Model):
    __tablename__ = "accounts"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    code = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    name_ar = db.Column(db.String(150))
    type = db.Column(db.Enum(AccountType), nullable=False)
    normal_side = db.Column(db.Enum(NormalSide), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("accounts.id"))
    is_active = db.Column(db.Boolean, default=True)
    # MARSOUD-COA-REBUILD — header accounts (is_postable=False) exist for
    # grouping + reporting only; post_journal refuses any line that lands
    # on them. Leaves (True) accept journals as normal.
    is_postable = db.Column(db.Boolean, default=True, nullable=False,
                             server_default="1")

    company = db.relationship("Company", backref=db.backref("accounts", lazy="dynamic"))
    children = db.relationship(
        "Account",
        backref=db.backref("parent", remote_side=[id]),
        lazy="select",
    )

    __table_args__ = (
        db.UniqueConstraint("company_id", "code", name="uq_company_account_code"),
    )

    @property
    def balance(self):
        """Net balance, excluding paused journal entries.

        For LEAF accounts (is_postable=True) this is just the sum of
        directly-posted lines. For HEADER accounts (is_postable=False)
        nothing posts here directly, so balance() walks the subtree and
        rolls up every descendant leaf. This is what the rest of the
        app already expected — reports + KPIs read .balance on parent
        codes like 1130 (AR) and need it to include every customer.
        """
        if not getattr(self, "is_postable", True):
            return self._rollup_balance()
        return self._direct_balance()

    def _direct_balance(self):
        from app.models.journal import JournalLine, JournalEntry
        from sqlalchemy import func
        result = db.session.query(
            func.coalesce(func.sum(JournalLine.debit_base), 0),
            func.coalesce(func.sum(JournalLine.credit_base), 0),
        ).select_from(JournalLine).join(JournalEntry).filter(
            JournalLine.account_id == self.id,
            JournalEntry.is_active.is_(True),
        ).first()
        debit, credit = float(result[0] or 0), float(result[1] or 0)
        if self.normal_side == NormalSide.DEBIT:
            return debit - credit
        return credit - debit

    def _rollup_balance(self):
        """Sum every descendant leaf's balance, normalized to this
        account's normal_side so the rolled-up number reads the same
        way the parent's own posting would have."""
        total = 0.0
        for child in self.children:
            child_bal = child.balance  # recursive — handles deep trees
            # Children carry the parent's normal_side by seed convention,
            # so we add directly. If a child's side ever diverges, this
            # mirror-flips so the sign stays correct.
            if child.normal_side == self.normal_side:
                total += child_bal
            else:
                total -= child_bal
        return total

    def __repr__(self):
        return f"<Account {self.code} {self.name}>"
