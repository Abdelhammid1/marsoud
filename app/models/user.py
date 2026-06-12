import enum
from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app import db


class UserStatus(str, enum.Enum):
    """HR-SS — accounts auto-provisioned for employees start as PENDING
    until OWNER activates them. DISABLED is a soft revoke.

    NB: kept in sync with is_active — PENDING/DISABLED imply is_active=False,
    ACTIVE implies is_active=True. Both checks work; status is the source
    of truth for the HR portal flow.
    """
    ACTIVE = "ACTIVE"
    PENDING = "PENDING"
    DISABLED = "DISABLED"

    @property
    def label_ar(self):
        return {"ACTIVE": "نشط",
                "PENDING": "قيد التفعيل",
                "DISABLED": "معطّل"}[self.value]

    @property
    def badge_class(self):
        return {"ACTIVE": "badge-paid",
                "PENDING": "badge-sent",
                "DISABLED": "badge-cancelled"}[self.value]


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
    password_hash = db.Column(db.String(255), nullable=False)
    locale = db.Column(db.String(5), default="ar")
    is_superadmin = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    last_login_at = db.Column(db.DateTime, nullable=True)
    # When this user is a client portal account, links them to a Customer row
    # in some company. Their role per-company will be "client".
    linked_customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)

    # HR-SS: lifecycle state + link to the employee record (if any).
    # Column exists in DB from migration l9f7d3a5b2c8.
    status = db.Column(db.String(20), default="ACTIVE", nullable=False)
    employee_id = db.Column(db.Integer,
                            db.ForeignKey("employees.id", ondelete="SET NULL"),
                            nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.now)

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
