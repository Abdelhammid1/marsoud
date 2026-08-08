"""MARSOUD-SUPERADMIN-CONTROL-01 T4 (2026-08-08) — per-tenant
feature grant / deny overrides.

Sits in the resolver's step-2 slot (see app/services/access.py::
can_access). Priority: platform FeatureFlag > DENY > GRANT >
plan module gate > role permission.

Why: today the only per-tenant lever is Company.plan_id. Every
"give this company X for a month" or "take away Y until they
pay" request forces us to invent a whole plan just for that one
row. This table lets the super-admin flip a feature per company
without touching the plan.

Storage shape mirrors FeatureFlag (module_key + reason + audit
timestamps) plus:
  · company_id      — scoped, not platform-wide
  · mode            — GRANT vs DENY (FeatureFlag is enabled/disabled)
  · expires_at      — optional; NULL = permanent, past = ignored
                       (row kept for audit trail; never auto-deleted)
  · reason          — NOT NULL, DB-level. The ticket calls out
                       'السبب إجباري' as a hard rule; the CHECK
                       here means even a direct-DB INSERT that
                       bypasses the service still refuses.
"""
from datetime import datetime
import sqlalchemy as sa
from app import db


class CompanyFeatureOverride(db.Model):
    __tablename__ = "company_feature_overrides"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(
        db.Integer,
        db.ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Registry code — validated in company_overrides.upsert_override
    # against feature_registry.all_modules(). Free string in the DB so
    # a code disappearing from the registry doesn't break the row
    # (get_override returns None for unknown codes — same fail-safe
    # shape FeatureFlag uses).
    feature_code = db.Column(db.String(60), nullable=False, index=True)
    mode = db.Column(db.String(8), nullable=False, index=True)
    reason = db.Column(db.Text, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True, index=True)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True,
    )
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow,
        server_default=sa.func.current_timestamp(),
    )

    __table_args__ = (
        db.UniqueConstraint("company_id", "feature_code",
                             name="uq_override_company_feature"),
        db.CheckConstraint("mode IN ('GRANT', 'DENY')",
                            name="ck_override_mode"),
    )
    company = db.relationship("Company")
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    @property
    def is_active(self):
        """True iff the row is currently in effect. Expired rows
        stick around for the audit trail but stop influencing
        access decisions."""
        if self.expires_at is None:
            return True
        return self.expires_at > datetime.utcnow()

    def __repr__(self):
        exp = ""
        if self.expires_at:
            exp = f" until {self.expires_at.isoformat()}"
        return (f"<CompanyFeatureOverride co={self.company_id} "
                f"{self.mode} {self.feature_code}{exp}>")
