"""Cycle 7 gap-close models — Documents, Notifications, ClientFeedback, AuditEntry.

These four sit on top of the CRM/Projects/Tasks tables to satisfy the
remaining qafr SRD requirements (file attachments, in-app notifications,
client feedback workflow, generic audit trail).
"""
import enum
from datetime import datetime
from app import db


class DocumentSourceType(str, enum.Enum):
    LEAD = "LEAD"
    PROJECT = "PROJECT"
    TASK = "TASK"
    # MARSOUD-CASH-CUSTODY-01 (2026-08-07) — settlement receipts
    # attach here (source_id = CashCustodySettlementLine.id). Uses
    # the same upload route + storage as the LEAD/PROJECT/TASK
    # attachments so a future doc-library scan sees custody
    # receipts without new plumbing.
    CASH_CUSTODY_SETTLEMENT = "CASH_CUSTODY_SETTLEMENT"
    # MARSOUD-ITEM-CUSTODY-01 (2026-08-07) — photos of the item at
    # handover/return + optional damage evidence. source_id points
    # at ItemCustody.id.
    ITEM_CUSTODY = "ITEM_CUSTODY"
    # MARSOUD-CUSTODY-REQUEST-APPROVE-01 (2026-08-10) — transfer-
    # receipt (bank slip / screenshot) attached to the request at
    # approval time. source_id = CashCustodyRequest.id. Tied to the
    # request row on purpose so the receipt stays as historical
    # proof even if the resulting custody is later cancelled or
    # reversed.
    CASH_CUSTODY_REQUEST = "CASH_CUSTODY_REQUEST"


class DocumentVisibility(str, enum.Enum):
    INTERNAL = "INTERNAL"   # staff only
    CLIENT = "CLIENT"       # also visible in the client portal


class Document(db.Model):
    __tablename__ = "documents"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    source_type = db.Column(db.String(20), nullable=False, index=True)
    source_id = db.Column(db.Integer, nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    file_path = db.Column(db.String(400), nullable=False)  # /static/docs/<co>/<id>.<ext>
    mimetype = db.Column(db.String(100))
    size_bytes = db.Column(db.Integer)
    visibility = db.Column(db.String(20), nullable=False, default="INTERNAL")
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id])


# ─── Notifications (in-app bell) ────────────────────────────────────────
class NotificationKind(str, enum.Enum):
    TASK_ASSIGNED = "TASK_ASSIGNED"
    TASK_DEADLINE_24H = "TASK_DEADLINE_24H"
    TASK_STATUS_CHANGED = "TASK_STATUS_CHANGED"
    TASK_COMMENT = "TASK_COMMENT"
    TASK_UPDATED = "TASK_UPDATED"
    PROJECT_DELIVERED = "PROJECT_DELIVERED"
    PROJECT_FEEDBACK_REQUESTED = "PROJECT_FEEDBACK_REQUESTED"
    PROJECT_FEEDBACK_SUBMITTED = "PROJECT_FEEDBACK_SUBMITTED"
    LEAD_STATUS_CHANGED = "LEAD_STATUS_CHANGED"
    MEETING_REMINDER = "MEETING_REMINDER"
    # MARSOUD-EMPLOYEE-DAILY-REPORTS
    DIGEST_DRAFT_READY = "DIGEST_DRAFT_READY"
    EMPLOYEE_REPORT_SUBMITTED = "EMPLOYEE_REPORT_SUBMITTED"
    # MARSOUD-MENTIONS — user @-mentioned inside a comment
    MENTION = "MENTION"
    # MARSOUD-CUSTOMER-BROADCAST-CENTER (Abdelhamid 2026-07-22)
    BROADCAST = "BROADCAST"
    # MARSOUD-PUBLIC-CONTACT-FORM-01 (Abdelhamid 2026-07-24)
    NEW_LEAD = "NEW_LEAD"
    # MARSOUD-VBILL-OVERDUE-01 (2026-08-06) — fires ONCE per bill when
    # cron flips POSTED/PARTIALLY_PAID → OVERDUE. The second cron run
    # finds the bill already OVERDUE and the "just flipped" set is
    # empty, so no dedup column is needed.
    VENDOR_BILL_OVERDUE = "VENDOR_BILL_OVERDUE"
    # MARSOUD-SUPERADMIN-CONTROL-01 T5 (2026-08-08) — quota
    # threshold crossed (80/90/100%). Fires via
    # `_send_owner_notification` in services/quotas.py after the
    # email dispatch. QuotaNotificationSent handles per-cycle
    # dedup, so no NotificationKind-level dedup needed.
    QUOTA_THRESHOLD = "QUOTA_THRESHOLD"


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    kind = db.Column(db.String(40), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text)
    link_url = db.Column(db.String(300))
    read_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)

    user = db.relationship("User", foreign_keys=[user_id])

    @property
    def is_read(self):
        return self.read_at is not None


# ─── ClientFeedback (gates project close) ───────────────────────────────
class ClientFeedback(db.Model):
    __tablename__ = "client_feedback"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer,
                           db.ForeignKey("projects.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"),
                            nullable=False)
    submitted_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    rating = db.Column(db.Integer, nullable=False)    # 1-5
    comment = db.Column(db.Text)
    approved = db.Column(db.Boolean, default=False, nullable=False)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    project = db.relationship("Project", foreign_keys=[project_id],
                              backref=db.backref("feedback", cascade="all, delete-orphan"))
    customer = db.relationship("Customer", foreign_keys=[customer_id])
    submitted_by = db.relationship("User", foreign_keys=[submitted_by_user_id])


# ─── Generic AuditEntry (every edit logged) ─────────────────────────────
class AuditEntry(db.Model):
    __tablename__ = "audit_entries"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    entity_type = db.Column(db.String(40), nullable=False, index=True)  # e.g. "Lead", "Project", "Task"
    entity_id = db.Column(db.Integer, nullable=False, index=True)
    action = db.Column(db.String(20), nullable=False)                   # CREATE / UPDATE / DELETE
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    changes_json = db.Column(db.Text)                                   # JSON {"field": ["old", "new"]}
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False, index=True)

    changed_by = db.relationship("User", foreign_keys=[changed_by_id])
