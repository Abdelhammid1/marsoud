"""MARSOUD-57.2 — Plan + SubscriptionReminderSent models."""
import json
from decimal import Decimal
from datetime import datetime
from app import db


# MARSOUD-MULTI-CURRENCY-PRICING (Abdelhamid 2026-07-22) — EGP is
# always the fallback so we don't need SAR/USD/AED rows populated to
# render a price.
DEFAULT_CURRENCY = "EGP"


class Plan(db.Model):
    """Commercial plan a company subscribes to.

    `allowed_modules` is a JSON list of coarse-grained module codes such as
    "accounting", "sales", "inventory". `has_permission()` checks the
    action's module against this list before answering True.
    """
    __tablename__ = "plans"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), nullable=False, unique=True)
    name_ar = db.Column(db.String(120), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    price_monthly = db.Column(db.Numeric(10, 2))
    price_yearly = db.Column(db.Numeric(10, 2))
    allowed_modules = db.Column(db.Text, nullable=False, default="[]")
    # MARSOUD-58 — per-sub-item gating within open sections.
    # NULL = back-compat = all sub-items allowed. A non-null JSON list
    # restricts which sub-items show + which routes are reachable.
    allowed_subitems = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def modules(self):
        try:
            data = json.loads(self.allowed_modules or "[]")
            return [m for m in data if isinstance(m, str)]
        except (ValueError, TypeError):
            return []

    def set_modules(self, modules):
        self.allowed_modules = json.dumps(list(modules))

    @property
    def subitems(self):
        """MARSOUD-58 — list of sub-item endpoint strings allowed by this
        plan. None means "no restriction" (back-compat); an empty list
        means "no sub-items at all"."""
        raw = self.allowed_subitems
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return [s for s in data if isinstance(s, str)]
        except (ValueError, TypeError):
            return None

    def set_subitems(self, items):
        if items is None:
            self.allowed_subitems = None
        else:
            self.allowed_subitems = json.dumps(list(items))

    # MARSOUD-MULTI-CURRENCY-PRICING — new resolver. All callers
    # displaying a price should call this instead of reading
    # price_monthly/price_yearly directly.
    def price_for(self, currency, cycle="monthly"):
        """Return the price for the requested currency + cycle.

        Order of lookup:
          1. plan_prices row (currency, cycle) — super-admin explicit
             per-currency price.
          2. If currency=EGP: fallback to legacy Plan.price_monthly /
             price_yearly (no schema break for existing rows).
          3. Otherwise: EGP row / legacy columns as the safe fallback
             so a client that picked SAR never sees an empty price.
        """
        cur = (currency or DEFAULT_CURRENCY).upper()
        row = PlanPrice.query.filter_by(
            plan_id=self.id, currency=cur).first()
        col = "price_monthly" if cycle == "monthly" else "price_yearly"
        if row and getattr(row, col) is not None:
            return Decimal(getattr(row, col))
        # Legacy fallback (EGP always has the legacy columns).
        legacy = self.price_monthly if cycle == "monthly" else self.price_yearly
        if legacy is not None:
            return Decimal(legacy)
        # Try an EGP row (in case a super-admin populated plan_prices
        # for EGP explicitly but left the legacy column NULL).
        if cur != DEFAULT_CURRENCY:
            egp_row = PlanPrice.query.filter_by(
                plan_id=self.id, currency=DEFAULT_CURRENCY).first()
            if egp_row and getattr(egp_row, col) is not None:
                return Decimal(getattr(egp_row, col))
        return None


class PlanPrice(db.Model):
    """MARSOUD-MULTI-CURRENCY-PRICING — one row per (plan_id, currency).
    Super-admin edits from /admin/plans/<id>/edit. NULL price_monthly
    / price_yearly means "not offered in this currency" — Plan.price_for
    falls back to the EGP row (or the legacy columns for EGP)."""
    __tablename__ = "plan_prices"
    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer,
                        db.ForeignKey("plans.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    currency = db.Column(db.String(3), nullable=False)
    price_monthly = db.Column(db.Numeric(15, 2), nullable=True)
    price_yearly = db.Column(db.Numeric(15, 2), nullable=True)

    __table_args__ = (
        db.UniqueConstraint("plan_id", "currency",
                             name="uq_plan_prices_plan_currency"),
    )


class SubscriptionReminderSent(db.Model):
    """Tracks which subscription-expiry reminder thresholds have been sent
    for each company so the cron doesn't re-send. Mirrors
    InvoiceReminderSent."""
    __tablename__ = "subscription_reminders_sent"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                            nullable=False, index=True)
    threshold_days = db.Column(db.Integer, nullable=False)
    expires_at_when_sent = db.Column(db.DateTime, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
