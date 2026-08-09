"""Platform-level audit log + super-admin impersonation trail.

These tables live at the platform (super-admin) layer and are populated
regardless of which company a user happens to be inside. Tenant code never
writes to them — only auth, the super-admin services, and the impersonation
flow do.
"""
from datetime import datetime
from app import db


class PlatformAuditLog(db.Model):
    __tablename__ = "platform_audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    action = db.Column(db.String(60), nullable=False, index=True)
    target_company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    ip_address = db.Column(db.String(45))
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    actor = db.relationship("User", foreign_keys=[actor_id])
    target_user = db.relationship("User", foreign_keys=[target_user_id])
    target_company = db.relationship("Company", foreign_keys=[target_company_id])


class PlatformError(db.Model):
    """Captured 500-class errors, scoped to the company the user was inside.

    Surfaced on /admin/companies/<id>/errors so support can see real failures
    when triaging a customer's issue.
    """
    __tablename__ = "platform_errors"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    route = db.Column(db.String(255))
    method = db.Column(db.String(10))
    status_code = db.Column(db.Integer)
    message = db.Column(db.Text)
    traceback = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    company = db.relationship("Company")
    user = db.relationship("User")


class PlatformCronRun(db.Model):
    """MARSOUD-SUPERADMIN-CONTROL-01 T11 (2026-08-08) — one row per
    (job_name, tick), populated by @track_cron_job wrapper in
    app/services/cron_tracking.py. Read by the ops-health page.

    Retention: latest 1000 rows per job_name kept; older wiped by
    the same tick's tail-trim. Rows are cheap (<200 bytes each) so
    steady-state is ~4.6 MB even with every job tracked.
    """
    __tablename__ = "platform_cron_runs"
    id = db.Column(db.Integer, primary_key=True)
    job_name = db.Column(db.String(80), nullable=False, index=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False, index=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    # ok | error | running (only "running" while inside the ctx mgr)
    status = db.Column(db.String(10), nullable=False, default="ok")
    # Raw JSON string of whatever the job returned (dict / int / None).
    summary_json = db.Column(db.Text, nullable=True)
    # First 500 chars of exception when status='error'.
    error_message = db.Column(db.Text, nullable=True)


class SuperadminImpersonation(db.Model):
    __tablename__ = "superadmin_impersonations"
    id = db.Column(db.Integer, primary_key=True)
    superadmin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ended_at = db.Column(db.DateTime)
    ip_address = db.Column(db.String(45))
    reason = db.Column(db.Text)

    superadmin = db.relationship("User", foreign_keys=[superadmin_id])
    company = db.relationship("Company")
