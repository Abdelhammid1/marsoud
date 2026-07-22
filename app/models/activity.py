"""MARSOUD-ACTLOG-01 — user sessions + activity log models."""
from datetime import datetime
from app import db


SESSION_STATUS = ("ACTIVE", "IDLE", "ENDED")

ACTION_TYPES = (
    "LOGIN", "LOGOUT",
    "VIEW",
    "CREATE", "UPDATE", "DELETE",
    "EXPORT", "APPROVE", "REJECT",
)


class UserSession(db.Model):
    """One row per browser session — created on login, kept warm by
    /auth/heartbeat, and flipped to IDLE / ENDED by the cron tick."""
    __tablename__ = "user_sessions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer,
                        db.ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    company_id = db.Column(db.Integer,
                           db.ForeignKey("companies.id"),
                           nullable=True, index=True)
    session_token = db.Column(db.String(80), nullable=False,
                              unique=True, index=True)
    login_at = db.Column(db.DateTime, default=datetime.utcnow,
                         nullable=False)
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow,
                             nullable=False)
    logout_at = db.Column(db.DateTime, nullable=True)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(500))
    device_type = db.Column(db.String(20))     # MOBILE / TABLET / DESKTOP
    device_os = db.Column(db.String(40))       # Windows / macOS / Android / iOS / Linux
    browser = db.Column(db.String(40))         # Chrome / Firefox / Safari / Edge / Other
    # MARSOUD-NEW-DEVICE (Abdelhamid 2026-07-22) — SHA-256[:32] of
    # (UA || ip_class). Used to decide whether to send the "new
    # device" alert on login. Non-unique — many sessions may share
    # a signature (same user on same laptop).
    device_signature = db.Column(db.String(64), nullable=True, index=True)
    status = db.Column(db.String(15), default="ACTIVE", nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])
    company = db.relationship("Company", foreign_keys=[company_id])

    @property
    def duration_seconds(self):
        end = self.logout_at or self.last_seen_at or datetime.utcnow()
        return int((end - self.login_at).total_seconds())


class UserActivityLog(db.Model):
    """Every significant action — VIEW, CREATE, UPDATE, etc."""
    __tablename__ = "user_activity_log"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                           db.ForeignKey("companies.id"),
                           nullable=True, index=True)
    user_id = db.Column(db.Integer,
                        db.ForeignKey("users.id"),
                        nullable=True, index=True)
    session_id = db.Column(db.Integer,
                           db.ForeignKey("user_sessions.id",
                                         ondelete="SET NULL"),
                           nullable=True, index=True)
    action_type = db.Column(db.String(20), nullable=False, index=True)
    entity_type = db.Column(db.String(40), nullable=True, index=True)
    entity_id = db.Column(db.Integer, nullable=True)
    entity_label = db.Column(db.String(255), nullable=True)
    route = db.Column(db.String(255), nullable=True)
    method = db.Column(db.String(10), nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    device_type = db.Column(db.String(20), nullable=True)
    device_os = db.Column(db.String(40), nullable=True)
    browser = db.Column(db.String(40), nullable=True)
    extra_data = db.Column(db.Text, nullable=True)   # JSON-encoded diff
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)

    company = db.relationship("Company", foreign_keys=[company_id])
    user = db.relationship("User", foreign_keys=[user_id])
    session = db.relationship("UserSession", foreign_keys=[session_id])
