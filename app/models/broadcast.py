"""MARSOUD-CUSTOMER-BROADCAST-CENTER (Abdelhamid 2026-07-22)."""
import json
from datetime import datetime
from app import db


AUDIENCE_ALL = "all"
AUDIENCE_TRIAL = "trial"
AUDIENCE_ACTIVE = "active"
AUDIENCE_EXPIRED = "expired"
AUDIENCE_BY_PLAN = "by_plan"


class Broadcast(db.Model):
    __tablename__ = "broadcasts"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body_html = db.Column(db.Text, nullable=False)
    audience_filter = db.Column(db.Text, nullable=False)
    channels = db.Column(db.String(40),
                         default="INAPP", nullable=False)
    sent_at = db.Column(db.DateTime, nullable=True)
    sent_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    target_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)

    @property
    def audience(self):
        try:
            return json.loads(self.audience_filter or "{}")
        except (ValueError, TypeError):
            return {}

    def set_audience(self, filter_dict):
        self.audience_filter = json.dumps(filter_dict or {})

    @property
    def channel_list(self):
        return [c for c in (self.channels or "").split(",") if c]

    def set_channels(self, channels):
        self.channels = ",".join(sorted(set(channels)))
