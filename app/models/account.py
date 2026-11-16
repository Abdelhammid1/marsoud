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

        Uses a per-request company-wide rollup cache (2 SQL queries total
        per company per request instead of hundreds of N+1 tree walks).
        """
        from flask import g, has_request_context
        if has_request_context() and getattr(self, "company_id", None) is not None and getattr(self, "id", None) is not None:
            cache = getattr(g, "_marsoud_acc_bal_cache", None)
            if cache is None:
                cache = {}
                g._marsoud_acc_bal_cache = cache
            cid = self.company_id
            if cid not in cache:
                cache[cid] = self._build_company_balance_map(cid)
            if self.id in cache[cid]:
                return cache[cid][self.id]
        if (not getattr(self, "is_postable", True)) or self.children:
            return self._rollup_balance()
        return self._direct_balance()

    @classmethod
    def _build_company_balance_map(cls, company_id):
        from app.models.journal import JournalLine, JournalEntry
        from sqlalchemy import func
        accs = cls.query.filter_by(company_id=company_id).all()
        if not accs:
            return {}
        acc_by_id = {a.id: a for a in accs}
        children_map = {a.id: [] for a in accs}
        for a in accs:
            if a.parent_id and a.parent_id in children_map:
                children_map[a.parent_id].append(a)

        raw_sums = db.session.query(
            JournalLine.account_id,
            func.coalesce(func.sum(JournalLine.debit_base), 0),
            func.coalesce(func.sum(JournalLine.credit_base), 0),
        ).join(JournalEntry, JournalEntry.id == JournalLine.entry_id).filter(
            JournalEntry.company_id == company_id,
            JournalEntry.is_active.is_(True),
        ).group_by(JournalLine.account_id).all()

        direct_raw = {row[0]: (float(row[1] or 0), float(row[2] or 0)) for row in raw_sums}
        memo = {}

        def _compute(acc_id):
            if acc_id in memo:
                return memo[acc_id]
            acc = acc_by_id[acc_id]
            kids = children_map.get(acc_id) or []
            if (not getattr(acc, "is_postable", True)) or kids:
                total = 0.0
                for k in kids:
                    k_bal = _compute(k.id)
                    if k.normal_side == acc.normal_side:
                        total += k_bal
                    else:
                        total -= k_bal
                memo[acc_id] = total
                return total
            debit, credit = direct_raw.get(acc_id, (0.0, 0.0))
            bal = (debit - credit) if acc.normal_side == NormalSide.DEBIT else (credit - debit)
            memo[acc_id] = bal
            return bal

        for a in accs:
            _compute(a.id)
        return memo

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


from sqlalchemy import event as _sa_event
@_sa_event.listens_for(db.session, "after_flush")
def _marsoud_invalidate_acc_bal_cache(session, flush_context):
    from flask import g, has_request_context
    if has_request_context() and hasattr(g, "_marsoud_acc_bal_cache"):
        delattr(g, "_marsoud_acc_bal_cache")
