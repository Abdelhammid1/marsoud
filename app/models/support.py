"""MARSOUD-SUPPORT-TICKETS-01 (Abdelhamid 2026-07-24).

Cross-tenant support ticketing. Customer companies open tickets;
Manasty support staff (with support.manage_tickets permission,
NOT super-admin) see and reply to all of them.
"""
from datetime import datetime
from app import db


# Status enum kept as constants + tuple so the admin form can render
# a select without importing Python enums into Jinja.
STATUS_OPEN = "OPEN"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_WAITING = "WAITING_ON_CUSTOMER"
STATUS_RESOLVED = "RESOLVED"
STATUS_CLOSED = "CLOSED"
ALL_STATUSES = (STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_WAITING,
                 STATUS_RESOLVED, STATUS_CLOSED)
STATUS_LABELS_AR = {
    STATUS_OPEN:        "مفتوحة",
    STATUS_IN_PROGRESS: "قيد المعالجة",
    STATUS_WAITING:     "بانتظار العميل",
    STATUS_RESOLVED:    "تم الحل",
    STATUS_CLOSED:      "مغلقة",
}

PRIORITY_LOW = "LOW"
PRIORITY_MEDIUM = "MEDIUM"
PRIORITY_HIGH = "HIGH"
PRIORITY_URGENT = "URGENT"
ALL_PRIORITIES = (PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH,
                   PRIORITY_URGENT)
PRIORITY_LABELS_AR = {
    PRIORITY_LOW: "منخفضة",
    PRIORITY_MEDIUM: "متوسطة",
    PRIORITY_HIGH: "عالية",
    PRIORITY_URGENT: "عاجلة",
}

ACTION_REPLY = "REPLY"
ACTION_INTERNAL = "INTERNAL_NOTE"
ACTION_STATUS = "STATUS_CHANGE"
ACTION_PRIORITY = "PRIORITY_CHANGE"
ACTION_ASSIGN = "ASSIGN"


class SupportTicket(db.Model):
    __tablename__ = "support_tickets"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), nullable=False,
                       default=STATUS_OPEN, index=True)
    priority = db.Column(db.String(20), nullable=False,
                         default=PRIORITY_MEDIUM)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                               nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)
    resolved_at = db.Column(db.DateTime, nullable=True)

    company = db.relationship("Company",
                               backref=db.backref("support_tickets",
                                                    lazy="dynamic"))
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    assigned_to = db.relationship("User",
                                    foreign_keys=[assigned_to_id])
    comments = db.relationship(
        "SupportTicketComment", backref="ticket",
        cascade="all, delete-orphan",
        order_by="SupportTicketComment.created_at",
    )
    audits = db.relationship(
        "SupportTicketAudit", backref="ticket",
        cascade="all, delete-orphan",
        order_by="SupportTicketAudit.created_at",
    )


class SupportTicketComment(db.Model):
    __tablename__ = "support_ticket_comments"
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer,
                          db.ForeignKey("support_tickets.id",
                                         ondelete="CASCADE"),
                          nullable=False, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False)
    content = db.Column(db.Text, nullable=False)
    attachment_url = db.Column(db.String(400))
    attachment_name = db.Column(db.String(200))
    is_internal = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])


class SupportTicketAudit(db.Model):
    __tablename__ = "support_ticket_audits"
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer,
                          db.ForeignKey("support_tickets.id",
                                         ondelete="CASCADE"),
                          nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                         nullable=False)
    action = db.Column(db.String(40), nullable=False)
    old_value = db.Column(db.String(200))
    new_value = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)

    actor = db.relationship("User", foreign_keys=[actor_id])
