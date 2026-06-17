"""Platform-level global key/value settings.

Used for super-admin-tunable values that affect every company:
  - subscription_reminder_thresholds  (CSV of ints)
  - subscription_grace_days           (int)
  - subscription_readonly_enabled     (true/false)
"""
from datetime import datetime
from app import db


class PlatformSetting(db.Model):
    __tablename__ = "platform_settings"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(60), nullable=False, unique=True)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                            onupdate=datetime.utcnow)
