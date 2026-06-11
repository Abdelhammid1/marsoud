"""MARSOUD-32 — Role + Permission models for custom granular permissions."""
from datetime import datetime
from app import db


# Many-to-many join table (no extra columns needed)
role_permissions = db.Table(
    "role_permissions",
    db.Column("role_id", db.Integer,
              db.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    db.Column("permission_id", db.Integer,
              db.ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)


class Permission(db.Model):
    """One row per action in Marsoud (e.g. invoices.create, journals.reverse).

    Seeded once at app boot from the canonical list. UI groups by
    `group_ar` and uses `label_ar` as the human-readable name.
    """
    __tablename__ = "permissions"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(80), nullable=False, unique=True, index=True)
    resource = db.Column(db.String(50), nullable=False, index=True)
    action = db.Column(db.String(50), nullable=False)
    group_ar = db.Column(db.String(80), nullable=False)
    label_ar = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    def __repr__(self):
        return f"<Permission {self.code}>"


class Role(db.Model):
    """Per-company role. SYSTEM roles are seeded automatically and are
    read-only in the UI; CUSTOM roles are owner-defined."""
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    # Short, machine-friendly identifier — matches the existing string
    # value in user_companies.role for system roles ('owner', 'admin', …).
    # For custom roles it's auto-generated as 'custom_<id>' on insert.
    code = db.Column(db.String(50), nullable=False)
    name_ar = db.Column(db.String(120), nullable=False)
    type = db.Column(db.String(20), nullable=False, default="CUSTOM")
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    company = db.relationship("Company", backref=db.backref("roles", lazy="dynamic"))
    permissions = db.relationship(
        "Permission",
        secondary=role_permissions,
        backref=db.backref("roles", lazy="dynamic"),
        lazy="select",
    )

    __table_args__ = (
        db.UniqueConstraint("company_id", "code", name="uq_role_company_code"),
    )

    @property
    def is_system(self):
        return self.type == "SYSTEM"

    @property
    def member_count(self):
        from app.models.user import user_companies
        return db.session.execute(
            user_companies.select().where(user_companies.c.role_id == self.id)
        ).rowcount

    def __repr__(self):
        return f"<Role {self.code} (co={self.company_id}, {self.type})>"
