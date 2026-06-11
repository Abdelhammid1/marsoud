import json
from datetime import datetime
from app import db


DEFAULT_REMINDER_CONFIG = {
    "enabled": True,
    "days_before": [7, 3],   # send N days before due_date
    "overdue_days": [0],     # send N days after due_date (0 = on due_date itself)
}


class Company(db.Model):
    __tablename__ = "companies"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    base_currency = db.Column(db.String(3), default="SAR", nullable=False)
    logo_url = db.Column(db.Text)
    logo_path = db.Column(db.String(300))   # uploaded logo on disk, served from /static/logos/
    address = db.Column(db.Text)
    tax_number = db.Column(db.String(50))
    vat_rate = db.Column(db.Numeric(5, 2), default=15.00)
    reminder_config = db.Column(db.Text)  # JSON: {enabled, days_before:[int], overdue_days:[int]}
    weekend_days = db.Column(db.String(20))  # CSV of Python weekday ints, "4,5" = Fri,Sat
    timezone = db.Column(db.String(50), default="Asia/Riyadh")
    parent_id = db.Column(db.Integer, db.ForeignKey("companies.id"))  # sub-company hierarchy
    is_active = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(20), default="ACTIVE", nullable=False)
    plan = db.Column(db.String(30), default="FREE", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

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

    def __repr__(self):
        return f"<Company {self.name}>"
