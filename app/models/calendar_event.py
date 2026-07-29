"""MARSOUD-CALENDAR-MANUAL-EVENTS (Abdelhamid 2026-07-29).

User-created events on /calendar/. The existing calendar aggregates
derived events (lead meetings / task deadlines / project deliveries).
This model backs the manual-add flow — a user posts a title +
start (+ optional end / description / location / reminder) and it
shows up in the timeline alongside the derived events.
"""
from datetime import datetime
from app import db


class CalendarEvent(db.Model):
    __tablename__ = "calendar_events"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                              nullable=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    starts_at = db.Column(db.DateTime, nullable=False, index=True)
    ends_at = db.Column(db.DateTime, nullable=True)
    location = db.Column(db.String(500), nullable=True)
    # Field is stored so a future reminder cron can consume it, but
    # no worker fires yet — flagged as a follow-up in the batch plan.
    reminder_minutes_before = db.Column(db.Integer, nullable=True)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False,
                           index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow, nullable=False)

    company = db.relationship("Company")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
