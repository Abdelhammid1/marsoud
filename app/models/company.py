import json
import re
from datetime import datetime
from app import db


# MARSOUD-SAAS-SUBDOMAIN — shared between register() and the nginx
# tenant-resolution middleware so the reserved list only lives in one place.
RESERVED_SUBDOMAINS = {"www", "api", "admin", "mail", "static", "cdn", "app"}
SUBDOMAIN_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])?$")


def is_valid_subdomain(value: str) -> bool:
    """3-63 chars, lowercase letters/digits/hyphens, not reserved."""
    if not value or value in RESERVED_SUBDOMAINS:
        return False
    if len(value) < 3 or len(value) > 63:
        return False
    return bool(SUBDOMAIN_PATTERN.match(value))


DEFAULT_REMINDER_CONFIG = {
    "enabled": True,
    "days_before": [7, 3],   # send N days before due_date
    "overdue_days": [0],     # send N days after due_date (0 = on due_date itself)
}


class Company(db.Model):
    __tablename__ = "companies"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    # MARSOUD-SAAS-SUBDOMAIN — tenant identity for *.marsoud.com routing.
    # Nullable until backfill script runs; enforced unique+indexed in DB.
    subdomain = db.Column(db.String(63), unique=True, nullable=True, index=True)
    base_currency = db.Column(db.String(3), default="SAR", nullable=False)
    logo_url = db.Column(db.Text)
    logo_path = db.Column(db.String(300))   # uploaded logo on disk, served from /static/logos/
    address = db.Column(db.Text)
    tax_number = db.Column(db.String(50))
    # MARSOUD-COMPANY-LEGAL — official metadata surfaced on every
    # invoice / quotation / receipt / email / PDF. All three nullable
    # so an existing tenant keeps working with just `name` until they
    # bother to fill them in. Templates read via the helper properties
    # (display_name / etc) so a missing value gracefully falls back
    # to `name`.
    legal_name = db.Column(db.String(200))
    brand_name = db.Column(db.String(150))
    commercial_register_no = db.Column(db.String(50))
    vat_rate = db.Column(db.Numeric(5, 2), default=15.00)
    reminder_config = db.Column(db.Text)  # JSON: {enabled, days_before:[int], overdue_days:[int]}
    weekend_days = db.Column(db.String(20))  # CSV of Python weekday ints, "4,5" = Fri,Sat
    timezone = db.Column(db.String(50), default="Asia/Riyadh")
    parent_id = db.Column(db.Integer, db.ForeignKey("companies.id"))  # sub-company hierarchy
    is_active = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(20), default="ACTIVE", nullable=False)
    plan = db.Column(db.String(30), default="FREE", nullable=False)
    # ERP-01 — when True, a sale that would overdraw stock is refused.
    stock_strict_mode = db.Column(db.Boolean, default=True, nullable=False)
    # ERP-03 — inventory + POS toggles. Columns exist in DB from migrations
    # m1a4e7c9b3f6 (stock_strict_mode) + o3c9f6d8e2a4 (the rest).
    cost_method = db.Column(db.String(10), default="AVERAGE", nullable=False)
    shift_required_for_pos = db.Column(db.Boolean, default=False, nullable=False)
    barcode_format = db.Column(db.String(20), default="CODE128", nullable=False)
    # MARSOUD-51 — bank info for the invoice PDF
    bank_name = db.Column(db.String(150))
    bank_account_holder = db.Column(db.String(150))
    bank_account_number = db.Column(db.String(50))
    iban = db.Column(db.String(50))
    # MARSOUD-57.2 + 57.3 — commercial plan + subscription window
    plan_id = db.Column(db.Integer, db.ForeignKey("plans.id"))
    subscription_started_at = db.Column(db.DateTime)
    subscription_expires_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # MARSOUD — soft-delete. Owners may close their own company; super-
    # admin can restore or wipe permanently from the admin panel.
    deleted_at = db.Column(db.DateTime, index=True)
    deleted_by_id = db.Column(db.Integer,
                              db.ForeignKey("users.id"), nullable=True)
    deletion_reason = db.Column(db.Text, nullable=True)

    # Named `subscription_plan` to avoid collision with the legacy `plan`
    # String column (kept for back-compat with the super-admin form).
    subscription_plan = db.relationship("Plan", foreign_keys=[plan_id])
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def is_fifo(self):
        return self.cost_method == "FIFO"

    @property
    def reminders(self):
        """Decoded reminder config with default fallback."""
        if not self.reminder_config:
            return dict(DEFAULT_REMINDER_CONFIG)
        try:
            cfg = json.loads(self.reminder_config)
        except (ValueError, TypeError):
            return dict(DEFAULT_REMINDER_CONFIG)
        out = dict(DEFAULT_REMINDER_CONFIG)
        out.update({k: v for k, v in cfg.items() if k in DEFAULT_REMINDER_CONFIG})
        return out

    def set_reminders(self, cfg):
        self.reminder_config = json.dumps(cfg)

    @property
    def rest_weekdays(self):
        """Set of Python weekday integers (Mon=0..Sun=6) that count as
        weekly rest. Defaults to {4, 5} (Fri/Sat) when unset — Gulf default.
        """
        if not self.weekend_days:
            return {4, 5}
        out = set()
        for piece in self.weekend_days.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                n = int(piece)
                if 0 <= n <= 6:
                    out.add(n)
            except ValueError:
                continue
        return out or {4, 5}

    parent = db.relationship("Company", remote_side=[id], backref="children")

    # ─── MARSOUD-COMPANY-LEGAL — official metadata helpers ────────────
    @property
    def display_name(self):
        """The name to show on customer-facing documents. Preference:
        brand_name (marketing identity) → legal_name (official) →
        the tenancy's built-in `name` field. Never returns None."""
        for candidate in (self.brand_name, self.legal_name, self.name):
            v = (candidate or "").strip()
            if v:
                return v
        return ""

    @property
    def official_name(self):
        """The name for signature blocks + tax filings. Preference:
        legal_name → name."""
        for candidate in (self.legal_name, self.name):
            v = (candidate or "").strip()
            if v:
                return v
        return ""

    def document_context(self):
        """One-shot dict of every company field a template might want
        to embed in an invoice / quotation / receipt / email / PDF.
        Keeps callers from re-copying the same 10 lines everywhere and
        gives us a single point to add new fields (e.g. license number)
        without editing every consumer."""
        return {
            "id": self.id,
            "name": (self.name or "").strip(),
            "display_name": self.display_name,
            "official_name": self.official_name,
            "legal_name": (self.legal_name or "").strip(),
            "brand_name": (self.brand_name or "").strip(),
            "commercial_register_no":
                (self.commercial_register_no or "").strip(),
            "tax_number": (self.tax_number or "").strip(),
            "address": (self.address or "").strip(),
            "base_currency": self.base_currency,
            "vat_rate": float(self.vat_rate or 0),
            "logo_url": self.logo_url,
            "logo_path": self.logo_path,
        }

    def __repr__(self):
        return f"<Company {self.name}>"
