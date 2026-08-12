import enum
from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app import db


class UserStatus(str, enum.Enum):
    """HR-SS — accounts auto-provisioned for employees start as PENDING
    until OWNER activates them. DISABLED is a soft revoke.

    MARSOUD-EMAIL-VERIFY (Abdelhamid 2026-07-22) — self-service signups
    now start as PENDING_VERIFICATION until the user clicks the verify
    link in the welcome email. They can log in but a middleware
    redirects them to /auth/verify-pending until they verify.

    NB: kept in sync with is_active — PENDING/DISABLED/
    PENDING_VERIFICATION imply is_active=False (users.py:83 property).
    """
    ACTIVE = "ACTIVE"
    PENDING = "PENDING"
    DISABLED = "DISABLED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"

    @property
    def label_ar(self):
        return {"ACTIVE": "نشط",
                "PENDING": "قيد التفعيل",
                "DISABLED": "معطّل",
                "PENDING_VERIFICATION": "بانتظار تفعيل البريد"}[self.value]

    @property
    def badge_class(self):
        return {"ACTIVE": "badge-paid",
                "PENDING": "badge-sent",
                "DISABLED": "badge-cancelled",
                "PENDING_VERIFICATION": "badge-sent"}[self.value]


user_companies = db.Table(
    "user_companies",
    db.Column("user_id", db.Integer, db.ForeignKey("users.id"), primary_key=True),
    db.Column("company_id", db.Integer, db.ForeignKey("companies.id"), primary_key=True),
    db.Column("role", db.String(20), default="owner"),
    db.Column("role_id", db.Integer, db.ForeignKey("roles.id"), nullable=True),
)


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(150), nullable=False)
    # MARSOUD-REGISTRATION-PHONES-01 (Batch 6 Ticket 4, 2026-07-29) —
    # personal contact number for the owner. Used by Manasty support
    # when email bounces. Free-text; no SMS verification yet.
    phone = db.Column(db.String(50), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    locale = db.Column(db.String(5), default="ar")
    is_superadmin = db.Column(db.Boolean, default=False, nullable=False)
    # MARSOUD-APPROVAL-GATED-SUPERADMIN (2026-08-12) — when True on
    # a user that ALSO has is_superadmin=True, every write attempt
    # under superadmin.* gets intercepted at the shared
    # @superadmin_required decorator and queued in
    # pending_superadmin_actions instead of executing. Only the
    # primary superadmin (this flag False) can decide the queue.
    # False for existing users — the migration adds the column
    # with server_default="0" so no accidental lock-out.
    requires_approval = db.Column(
        db.Boolean, default=False, nullable=False,
    )
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    last_login_at = db.Column(db.DateTime, nullable=True)
    # When this user is a client portal account, links them to a Customer row
    # in some company. Their role per-company will be "client".
    linked_customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)

    # HR-SS: lifecycle state.
    status = db.Column(db.String(20), default="ACTIVE", nullable=False)

    # MARSOUD-EMAIL-VERIFY (Abdelhamid 2026-07-22) — set on successful
    # click of the verify-email link. Nullable → not yet verified.
    email_verified_at = db.Column(db.DateTime, nullable=True)

    # MARSOUD-LOCKOUT-RESET (Abdelhamid 2026-07-22) — brute-force
    # protection. failed_login_attempts is incremented on wrong pw and
    # reset to 0 on success. locked_until, when in the future, refuses
    # every login attempt with a friendly Arabic error.
    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)

    # MARSOUD-TERMS-CONSENT (Abdelhamid 2026-07-22) — legal audit
    # trail. When the super-admin publishes a new version via
    # /admin/legal, middleware nudges any user whose stored version
    # doesn't match to re-accept on their next request.
    terms_accepted_at = db.Column(db.DateTime, nullable=True)
    terms_version = db.Column(db.String(20), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    companies = db.relationship(
        "Company",
        secondary=user_companies,
        backref=db.backref("users", lazy="dynamic"),
        lazy="select",
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def status_enum(self):
        try:
            return UserStatus(self.status or "ACTIVE")
        except ValueError:
            return UserStatus.ACTIVE

    @property
    def is_pending(self):
        return self.status == UserStatus.PENDING.value

    @property
    def is_disabled(self):
        return self.status == UserStatus.DISABLED.value

    @property
    def is_pending_verification(self):
        """MARSOUD-EMAIL-VERIFY — used by the auth middleware to send
        the user to /auth/verify-pending instead of the dashboard."""
        return self.status == UserStatus.PENDING_VERIFICATION.value
