"""MARSOUD-FEATURE-FLAGS-KILL-SWITCH (Abdelhamid 2026-07-22).

Runtime module toggle. One row per module_key (e.g. "payroll",
"manufacturing", "inventory"). Super-admin sets `enabled=False`
+ `disabled_reason` to take a module offline instantly.
"""
from datetime import datetime
from app import db


class FeatureFlag(db.Model):
    __tablename__ = "feature_flags"

    id = db.Column(db.Integer, primary_key=True)
    module_key = db.Column(db.String(60), nullable=False, unique=True)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    disabled_reason = db.Column(db.Text)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    updated_by = db.relationship("User", foreign_keys=[updated_by_id])
