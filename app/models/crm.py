"""CRM + Projects + Tasks models — Cycle 7 (ported from qafr-opsflow).

Integrated as a native Marsoud module: every table is `company_id`-scoped
for multi-tenant isolation, reuses Marsoud's existing `User` (for staff /
assignees / managers) and `Customer` (for project clients) instead of a
parallel user system. Lead → Won → "Convert to project" auto-creates a
Marsoud `Customer` row tied to the project.
"""
import enum
from datetime import datetime, date, timedelta
from decimal import Decimal
from app import db


# ─── CRM ────────────────────────────────────────────────────────────────
class LeadStatus(enum.Enum):
    NEW_LEAD = "NEW_LEAD"
    CONTACTED = "CONTACTED"
    MEETING_SCHEDULED = "MEETING_SCHEDULED"
    NEGOTIATION = "NEGOTIATION"
    PROPOSAL_SENT = "PROPOSAL_SENT"
    WON = "WON"
    LOST = "LOST"

    @property
    def label_ar(self):
        return {
            "NEW_LEAD":          "عميل جديد",
            "CONTACTED":         "تم التواصل",
            "MEETING_SCHEDULED": "اجتماع مجدول",
            "NEGOTIATION":       "تفاوض",
            "PROPOSAL_SENT":     "أُرسل العرض",
            "WON":               "ربحناها",
            "LOST":              "خسرناها",
        }[self.value]

    @property
    def badge_class(self):
        # Reuses Marsoud's existing badge palette.
        return {
            "NEW_LEAD":          "badge-draft",
            "CONTACTED":         "badge-sent",
            "MEETING_SCHEDULED": "badge-sent",
            "NEGOTIATION":       "badge-partial",
            "PROPOSAL_SENT":     "badge-partial",
            "WON":               "badge-paid",
            "LOST":              "badge-cancelled",
        }[self.value]


class LeadType(enum.Enum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"
    REFERRAL = "REFERRAL"
    EXISTING_CLIENT = "EXISTING_CLIENT"

    @property
    def label_ar(self):
        return {
            "INBOUND":         "Inbound (هو اللي جه بنفسه)",
            "OUTBOUND":        "Outbound (إحنا اللي وصلناله)",
            "REFERRAL":        "Referral (ترشيح)",
            "EXISTING_CLIENT": "Existing Client (عميل سابق)",
        }[self.value]


class LeadSource(enum.Enum):
    LINKEDIN = "LINKEDIN"
    FACEBOOK = "FACEBOOK"
    GOOGLE_SEARCH = "GOOGLE_SEARCH"
    WEBSITE = "WEBSITE"
    WHATSAPP = "WHATSAPP"
    COLD_CALL = "COLD_CALL"
    EMAIL_OUTREACH = "EMAIL_OUTREACH"
    EVENT = "EVENT"
    OTHER = "OTHER"

    @property
    def label_ar(self):
        return {
            "LINKEDIN":       "LinkedIn",
            "FACEBOOK":       "Facebook",
            "GOOGLE_SEARCH":  "Google Search",
            "WEBSITE":        "Website",
            "WHATSAPP":       "WhatsApp",
            "COLD_CALL":      "Cold Call",
            "EMAIL_OUTREACH": "Email Outreach",
            "EVENT":          "Event",
            "OTHER":          "أخرى",
        }[self.value]


class Lead(db.Model):
    __tablename__ = "leads"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    # PER-CO-NUMBERING (Abdelhamid 2026-07-04) — per-company display
    # sequence like "L-0001". Nullable until backfill runs.
    number = db.Column(db.String(30), nullable=True, index=True)
    client_name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(200))
    phone = db.Column(db.String(30), nullable=False)
    service_needed = db.Column(db.String(200), nullable=False)
    lead_type = db.Column(db.String(20))
    source = db.Column(db.String(100))

    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                               nullable=False)
    # MARSOUD-44 follow-up — who CREATED the lead (vs the rep it's assigned to)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=True)
    status = db.Column(db.Enum(LeadStatus),
                       default=LeadStatus.NEW_LEAD, nullable=False, index=True)

    next_meeting = db.Column(db.DateTime)
    meeting_notes = db.Column(db.Text)
    notes = db.Column(db.Text)  # MARSOUD-46 follow-up — generic optional notes
    # MARSOUD-63 — two optional long-text fields for capturing the lead's
    # actual request and the action sales needs to take on it.
    request_description = db.Column(db.Text)
    sales_action_required = db.Column(db.Text)
    # MARSOUD-DASH-01 — expected deal size if the lead converts. Summed
    # across open leads on the owner dashboard's pipeline card.
    expected_value = db.Column(db.Numeric(15, 2), nullable=True)
    # MARSOUD-CRM-EXPANSION §2 — which marketing campaign brought this
    # lead in. Nullable so legacy leads + walk-ins don't break.
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaigns.id"),
                             nullable=True, index=True)

    quotation_path = db.Column(db.Text)
    contract_path = db.Column(db.Text)

    lost_reason = db.Column(db.Text)
    converted_at = db.Column(db.DateTime)
    # On conversion, link to the Customer we auto-created (Marsoud's model).
    converted_customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)
    # MARSOUD-47 — soft delete (OWNER + admin only)
    deleted_at = db.Column(db.DateTime, nullable=True)
    deleted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=True)

    company = db.relationship("Company")
    assigned_to = db.relationship("User", foreign_keys=[assigned_to_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    converted_customer = db.relationship("Customer", foreign_keys=[converted_customer_id])
    deleted_by = db.relationship("User", foreign_keys=[deleted_by_id])
    campaign = db.relationship("Campaign", foreign_keys=[campaign_id])
    history = db.relationship(
        "LeadStatusEvent", back_populates="lead",
        cascade="all, delete-orphan", order_by="LeadStatusEvent.created_at",
    )

    @property
    def is_converted(self):
        return self.converted_at is not None

    @property
    def is_open(self):
        return self.status not in (LeadStatus.WON, LeadStatus.LOST)


class LeadStatusEvent(db.Model):
    __tablename__ = "lead_status_events"
    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    from_status = db.Column(db.Enum(LeadStatus))
    to_status = db.Column(db.Enum(LeadStatus), nullable=False)
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    lead = db.relationship("Lead", back_populates="history")
    changed_by = db.relationship("User", foreign_keys=[changed_by_id])


# ─── Projects ───────────────────────────────────────────────────────────
class ProjectStatus(enum.Enum):
    PLANNING = "PLANNING"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEW = "REVIEW"
    DELIVERED = "DELIVERED"
    CLIENT_FEEDBACK = "CLIENT_FEEDBACK"
    CLOSED = "CLOSED"

    @property
    def label_ar(self):
        return {
            "PLANNING":        "تخطيط",
            "IN_PROGRESS":     "قيد التنفيذ",
            "REVIEW":          "مراجعة",
            "DELIVERED":       "مُسلَّم",
            "CLIENT_FEEDBACK": "في انتظار ملاحظات العميل",
            "CLOSED":          "مكتمل",
        }[self.value]

    @property
    def badge_class(self):
        return {
            "PLANNING":        "badge-draft",
            "IN_PROGRESS":     "badge-sent",
            "REVIEW":          "badge-partial",
            "DELIVERED":       "badge-paid",
            "CLIENT_FEEDBACK": "badge-partial",
            "CLOSED":          "badge-paid",
        }[self.value]


# Allowed status transitions — gated in the route layer.
PROJECT_TRANSITIONS = {
    ProjectStatus.PLANNING:        [ProjectStatus.IN_PROGRESS],
    ProjectStatus.IN_PROGRESS:     [ProjectStatus.REVIEW, ProjectStatus.PLANNING],
    ProjectStatus.REVIEW:          [ProjectStatus.IN_PROGRESS, ProjectStatus.DELIVERED],
    ProjectStatus.DELIVERED:       [ProjectStatus.CLIENT_FEEDBACK, ProjectStatus.REVIEW],
    ProjectStatus.CLIENT_FEEDBACK: [ProjectStatus.CLOSED, ProjectStatus.DELIVERED],
    ProjectStatus.CLOSED:          [],
}


class Project(db.Model):
    __tablename__ = "projects"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    # PER-CO-NUMBERING (Abdelhamid 2026-07-04) — per-company display
    # sequence like "PRJ-0001". Nullable until backfill runs.
    number = db.Column(db.String(30), nullable=True, index=True)
    name = db.Column(db.String(200), nullable=False)
    lead_id = db.Column(db.Integer, db.ForeignKey("leads.id"))
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"),
                            nullable=False)
    type = db.Column(db.String(100), nullable=False)
    manager_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.Enum(ProjectStatus),
                       default=ProjectStatus.PLANNING, nullable=False, index=True)
    progress_pct = db.Column(db.Numeric(5, 2), default=Decimal("0.00"), nullable=False)
    notes = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)
    # MARSOUD — soft-delete on projects (Ticket L). Owner-only delete.
    deleted_at = db.Column(db.DateTime, index=True)
    deleted_by_id = db.Column(db.Integer,
                              db.ForeignKey("users.id"), nullable=True)
    deletion_reason = db.Column(db.Text, nullable=True)

    company = db.relationship("Company")
    lead = db.relationship("Lead", foreign_keys=[lead_id])
    customer = db.relationship("Customer", foreign_keys=[customer_id])
    manager = db.relationship("User", foreign_keys=[manager_id])
    deleted_by = db.relationship("User", foreign_keys=[deleted_by_id])
    members = db.relationship(
        "ProjectMember", back_populates="project",
        cascade="all, delete-orphan",
    )
    milestones = db.relationship(
        "Milestone", back_populates="project",
        cascade="all, delete-orphan", order_by="Milestone.order",
    )
    status_events = db.relationship(
        "ProjectStatusEvent", back_populates="project",
        cascade="all, delete-orphan", order_by="ProjectStatusEvent.created_at",
    )

    def can_transition_to(self, target):
        return target in PROJECT_TRANSITIONS.get(self.status, [])

    def recompute_progress(self):
        """Recompute progress_pct from task completion ratio."""
        total = Task.query.filter_by(project_id=self.id).count()
        if total == 0:
            self.progress_pct = Decimal("0.00")
            return
        done = Task.query.filter_by(
            project_id=self.id, status=TaskStatus.DONE,
        ).count()
        self.progress_pct = (Decimal(done) / Decimal(total) * 100).quantize(Decimal("0.01"))


class ProjectMember(db.Model):
    __tablename__ = "project_members"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer,
                           db.ForeignKey("projects.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    project = db.relationship("Project", back_populates="members")
    user = db.relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        db.UniqueConstraint("project_id", "user_id", name="uq_project_member"),
    )


class Milestone(db.Model):
    __tablename__ = "milestones"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer,
                           db.ForeignKey("projects.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    target_date = db.Column(db.Date)
    order = db.Column(db.Integer, default=0, nullable=False)
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    project = db.relationship("Project", back_populates="milestones")

    @property
    def is_done(self):
        return self.completed_at is not None


class ProjectStatusEvent(db.Model):
    __tablename__ = "project_status_events"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer,
                           db.ForeignKey("projects.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    from_status = db.Column(db.Enum(ProjectStatus))
    to_status = db.Column(db.Enum(ProjectStatus), nullable=False)
    changed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    project = db.relationship("Project", back_populates="status_events")
    changed_by = db.relationship("User", foreign_keys=[changed_by_id])


# ─── Tasks ──────────────────────────────────────────────────────────────
class TaskStatus(enum.Enum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEW = "REVIEW"
    DONE = "DONE"
    BLOCKED = "BLOCKED"

    @property
    def label_ar(self):
        return {
            "TODO":        "للقيام",
            "IN_PROGRESS": "قيد التنفيذ",
            "REVIEW":      "مراجعة",
            "DONE":        "مكتملة",
            "BLOCKED":     "متوقفة",
        }[self.value]

    @property
    def badge_class(self):
        return {
            "TODO":        "badge-draft",
            "IN_PROGRESS": "badge-sent",
            "REVIEW":      "badge-partial",
            "DONE":        "badge-paid",
            "BLOCKED":     "badge-cancelled",
        }[self.value]


KANBAN_ORDER = [
    TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW,
    TaskStatus.DONE, TaskStatus.BLOCKED,
]


class TaskPriority(enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"

    @property
    def label_ar(self):
        return {
            "LOW": "منخفضة", "MEDIUM": "متوسطة",
            "HIGH": "عالية", "URGENT": "عاجلة",
        }[self.value]

    @property
    def badge_class(self):
        return {
            "LOW":    "badge-draft",
            "MEDIUM": "badge-sent",
            "HIGH":   "badge-partial",
            "URGENT": "badge-overdue",
        }[self.value]


class Task(db.Model):
    __tablename__ = "tasks"
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)

    # MARSOUD-27: nullable so tasks can exist without a project
    project_id = db.Column(db.Integer,
                           db.ForeignKey("projects.id", ondelete="CASCADE"),
                           nullable=True, index=True)
    milestone_id = db.Column(db.Integer,
                             db.ForeignKey("milestones.id", ondelete="SET NULL"))
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                               nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=True)

    priority = db.Column(db.Enum(TaskPriority),
                         default=TaskPriority.MEDIUM, nullable=False)
    status = db.Column(db.Enum(TaskStatus),
                       default=TaskStatus.TODO, nullable=False, index=True)

    deadline = db.Column(db.Date)
    notes = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)
    completed_at = db.Column(db.DateTime)
    # MARSOUD-TASK-ARCHIVE-01 — soft archive. When set, the task is
    # hidden from every Kanban / dashboard / stats view but preserved
    # with comments + attachments + activity log intact. Restoring just
    # NULLs both columns.
    archived_at = db.Column(db.DateTime, index=True)
    archived_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                                nullable=True)

    company = db.relationship("Company")
    project = db.relationship("Project", foreign_keys=[project_id], backref="tasks")
    milestone = db.relationship("Milestone", foreign_keys=[milestone_id])
    archived_by = db.relationship("User", foreign_keys=[archived_by_id])
    assigned_to = db.relationship("User", foreign_keys=[assigned_to_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    @property
    def is_overdue(self):
        """Overdue when deadline passed and not done/blocked."""
        if not self.deadline or self.status in (TaskStatus.DONE, TaskStatus.BLOCKED):
            return False
        return self.deadline < date.today()

    @property
    def deadline_soon(self):
        """True when deadline is today or within the next 24h."""
        if not self.deadline or self.status in (TaskStatus.DONE, TaskStatus.BLOCKED):
            return False
        today = date.today()
        return today <= self.deadline <= today + timedelta(days=1)

    @property
    def all_assignees(self):
        """Union of multi-assignee rows + the legacy primary assignee."""
        out = list(self.assignees) if self.assignees else []
        if self.assigned_to and self.assigned_to not in out:
            out.insert(0, self.assigned_to)
        return out

    @property
    def assignee_ids(self):
        return [u.id for u in self.all_assignees]


# ─── TASKS-02 multi-assignee + comments + activity log ──────────────────
task_assignees = db.Table(
    "task_assignees",
    db.Column("task_id", db.Integer,
              db.ForeignKey("tasks.id", ondelete="CASCADE"),
              primary_key=True),
    db.Column("user_id", db.Integer,
              db.ForeignKey("users.id"), primary_key=True),
    db.Column("assigned_by_id", db.Integer, db.ForeignKey("users.id")),
    db.Column("assigned_at", db.DateTime, default=datetime.utcnow, nullable=False),
)

Task.assignees = db.relationship(
    "User", secondary=task_assignees, lazy="select",
    foreign_keys=[task_assignees.c.task_id, task_assignees.c.user_id],
)


class LeadComment(db.Model):
    """MARSOUD — discussion thread attached to a Lead. Mirrors TaskComment
    so the detail-page UI feels familiar."""
    __tablename__ = "lead_comments"
    id = db.Column(db.Integer, primary_key=True)
    lead_id = db.Column(db.Integer,
                        db.ForeignKey("leads.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    content = db.Column(db.Text, nullable=False)
    attachment_url = db.Column(db.String(400))
    attachment_name = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    lead = db.relationship(
        "Lead", foreign_keys=[lead_id],
        backref=db.backref("comments",
                           order_by="LeadComment.created_at.asc()",
                           cascade="all, delete-orphan"))
    user = db.relationship("User", foreign_keys=[user_id])


class TaskComment(db.Model):
    __tablename__ = "task_comments"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer,
                        db.ForeignKey("tasks.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    content = db.Column(db.Text, nullable=False)
    attachment_url = db.Column(db.String(400))
    attachment_name = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    task = db.relationship(
        "Task", foreign_keys=[task_id],
        backref=db.backref("comments",
                           order_by="TaskComment.created_at.asc()",
                           cascade="all, delete-orphan"))
    user = db.relationship("User", foreign_keys=[user_id])


class TaskActivityLog(db.Model):
    __tablename__ = "task_activity_logs"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer,
                        db.ForeignKey("tasks.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    action = db.Column(db.String(60), nullable=False)
    before_json = db.Column(db.Text)
    after_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    task = db.relationship(
        "Task", foreign_keys=[task_id],
        backref=db.backref("activity",
                           order_by="TaskActivityLog.created_at.desc()",
                           cascade="all, delete-orphan"))
    user = db.relationship("User", foreign_keys=[user_id])
