from datetime import datetime, timedelta
from app import db


def _default_expiry():
    return datetime.utcnow() + timedelta(days=7)


class Invitation(db.Model):
    """One-time-use email invitation to join a company with a specific role.

    Token is generated via itsdangerous so it can be verified without a DB hit,
    but we also persist the row so the invite can be revoked and audited.
    """
    __tablename__ = "invitations"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    email = db.Column(db.String(150), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False)
    token = db.Column(db.String(255), unique=True, nullable=False, index=True)
    invited_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    expires_at = db.Column(db.DateTime, default=_default_expiry, nullable=False)
    accepted_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    company = db.relationship("Company")
    invited_by = db.relationship("User")

    @property
    def is_pending(self):
        return (
            self.accepted_at is None
            and self.revoked_at is None
            and self.expires_at > datetime.utcnow()
        )
