"""MARSOUD-USER-FILES — per-user private file storage.

Each row is one file owned by a single user, scoped to a company so
multi-tenancy works. Files sit outside the public static/ tree — they
must be streamed through a gated route (see app/routes/user_files.py)
so ownership + admin-visibility rules are enforced on every read.
"""
from datetime import datetime
from app import db


class UserFile(db.Model):
    __tablename__ = "user_files"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                            nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                          nullable=False, index=True)
    # Original filename as uploaded — used for display + Content-Disposition
    # on the download route.
    name = db.Column(db.String(255), nullable=False)
    # Relative path under app/private_uploads/user_files/. Kept as a
    # string so it survives instance moves that change the absolute
    # root; the streaming helper prefixes app.root_path at read time.
    storage_key = db.Column(db.String(400), nullable=False)
    mimetype = db.Column(db.String(120))
    size_bytes = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                             nullable=False, index=True)

    company = db.relationship("Company")
    user = db.relationship("User", backref=db.backref(
        "user_files", cascade="all, delete-orphan", lazy="dynamic"))

    @property
    def is_previewable(self):
        """PDFs and common image mimetypes render inline in-app; every
        other extension gets a download button in the UI."""
        m = (self.mimetype or "").lower()
        return (
            m == "application/pdf"
            or m.startswith("image/")
        )

    @property
    def size_display(self):
        """Human-readable size — KB up to 1 MB, then MB."""
        n = int(self.size_bytes or 0)
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n / (1024 * 1024):.1f} MB"
