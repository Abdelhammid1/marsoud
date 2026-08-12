"""MARSOUD-APPROVAL-GATED-SUPERADMIN (2026-08-12) — queued
superadmin actions waiting on primary approval.

Every write attempt on `superadmin.*` from a user with
`requires_approval=True` lands here instead of executing.
The primary superadmin (`requires_approval=False`) approves
or rejects from /admin/pending-actions. Approval replays the
original request via `test_request_context` + the endpoint's
view function with `g._approval_bypass=True` so the gate
doesn't re-fire.

Schema notes:
  · `actor_id` FK is ondelete=RESTRICT — a user with pending
    actions can't be deleted. Forces the primary to decide
    the queue first, preserving audit trail.
  · `staged_files` stores a JSON dict {form_field: disk_path}
    for uploaded files that were parked under
    static/staging/pending_actions/ at queue-time. The
    executor opens each on approve + cleans up after. Reject
    also cleans up (no reason to keep them).
  · `status` CHECK constraint enforces the three-value enum
    at the DB level so a bad UPDATE can't silently corrupt
    the queue.
"""
from datetime import datetime
import sqlalchemy as sa
from app import db


class PendingSuperadminAction(db.Model):
    __tablename__ = "pending_superadmin_actions"
    __table_args__ = (
        db.CheckConstraint(
            "status IN ('pending','approved','rejected')",
            name="ck_pending_action_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="RESTRICT",
                       name="fk_pending_action_actor"),
        nullable=False, index=True,
    )
    endpoint = db.Column(db.String(120), nullable=False, index=True)
    method = db.Column(db.String(8), nullable=False)
    url_path = db.Column(db.String(500), nullable=False)
    view_args = db.Column(db.Text)       # JSON — Flask view_args dict
    form_data = db.Column(db.Text)       # JSON — {field: [values]}
    staged_files = db.Column(db.Text)    # JSON — {field: staging_path}
    status = db.Column(db.String(16), nullable=False,
                        default="pending", index=True)
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow,
        server_default=sa.func.current_timestamp(),
        nullable=False, index=True,
    )
    decided_by = db.Column(
        db.Integer,
        db.ForeignKey("users.id", name="fk_pending_action_decider"),
        nullable=True,
    )
    decided_at = db.Column(db.DateTime, nullable=True)
    decision_note = db.Column(db.Text, nullable=True)

    actor = db.relationship("User", foreign_keys=[actor_id])
    decider = db.relationship("User", foreign_keys=[decided_by])

    @property
    def is_pending(self):
        return self.status == "pending"

    def __repr__(self):
        return (f"<PendingSuperadminAction id={self.id} "
                f"endpoint={self.endpoint!r} status={self.status}>")
