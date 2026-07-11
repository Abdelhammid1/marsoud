"""MARSOUD-TASK-SCHEDULE (Abdelhamid 2026-07-11) — schedule a task for
a future date, and/or repeat it daily until an end date.

Two tickets combined into one lightweight model:

  Ticket 37: "عاوز اقدر اعمل مهمة تتكرر يوميا لمدة معينة"
             → recurrence: DAILY, with a start + end date.
  Ticket 38: "عاوز اقدر اعمل schedule لمهمة"
             → one-off task that activates on a future date.

Design:
  · The schedule stores the full task template (title, description,
    priority, project, milestone, assignees).
  · A daily cron tick calls `materialize_due_schedules()` which
    inspects every ACTIVE row and creates a fresh Task when today
    falls inside the [start_date, end_date] window AND hasn't
    already been created today (dedupe via last_generated_date).
  · A ONCE schedule flips to inactive as soon as it fires once.
  · A DAILY schedule flips to inactive when today > end_date.

We don't touch the existing Task table — a schedule is a *template*
that produces regular Task rows. Every generated Task looks and
behaves exactly like a task created by hand (assignees notified,
activity log created, etc.).
"""
from datetime import datetime

from app import db


# Multi-assignee join table for the schedule template — mirrors the
# existing `task_assignees` shape so cloning the assignee list is a
# simple copy from one table into the other at generation time.
task_schedule_assignees = db.Table(
    "task_schedule_assignees",
    db.Column("schedule_id", db.Integer,
              db.ForeignKey("task_schedules.id", ondelete="CASCADE"),
              primary_key=True),
    db.Column("user_id", db.Integer,
              db.ForeignKey("users.id"), primary_key=True),
)


# Recurrence kinds. Kept as short strings (no Enum column) so a
# future kind can be added without a migration or an alembic ALTER
# on the enum type — SQLite in particular is fussy about that.
RECURRENCE_ONCE = "ONCE"      # one-time future activation
RECURRENCE_DAILY = "DAILY"    # daily recurring within [start, end]
RECURRENCE_KINDS = (RECURRENCE_ONCE, RECURRENCE_DAILY)


class TaskSchedule(db.Model):
    __tablename__ = "task_schedules"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)

    # ─── Task template (copied verbatim into each generated Task) ──
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    priority = db.Column(db.String(20), default="MEDIUM", nullable=False)
    project_id = db.Column(db.Integer,
                           db.ForeignKey("projects.id", ondelete="CASCADE"))
    milestone_id = db.Column(db.Integer,
                             db.ForeignKey("milestones.id", ondelete="SET NULL"))
    notes = db.Column(db.Text)

    # Legacy single-assignee slot — always populated with the primary
    # assignee (whoever appears first in the schedule form). Kept
    # separate from the M2M table for symmetry with `tasks.assigned_to_id`.
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                               nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=False)

    # ─── Recurrence config ────────────────────────────────────────
    recurrence = db.Column(db.String(20), nullable=False,
                           default=RECURRENCE_ONCE)
    # First date on which a Task should be generated. For DAILY
    # schedules this is also the recurrence start.
    start_date = db.Column(db.Date, nullable=False)
    # For DAILY — inclusive end. NULL means "no end" (not exposed in
    # the UI in the MVP; reserved for a future "until cancelled" mode).
    end_date = db.Column(db.Date, nullable=True)

    # ─── Bookkeeping ──────────────────────────────────────────────
    active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    last_generated_date = db.Column(db.Date, nullable=True)
    generated_count = db.Column(db.Integer, default=0, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    # ─── Relationships ────────────────────────────────────────────
    company = db.relationship("Company")
    project = db.relationship("Project")
    milestone = db.relationship("Milestone")
    assigned_to = db.relationship("User", foreign_keys=[assigned_to_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    assignees = db.relationship(
        "User", secondary=task_schedule_assignees, lazy="select",
    )

    def __repr__(self):
        return (f"<TaskSchedule {self.id} {self.recurrence} "
                f"'{self.title}' co={self.company_id}>")
