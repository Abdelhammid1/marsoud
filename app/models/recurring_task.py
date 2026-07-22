"""MARSOUD-RECURRING-TASKS (Abdelhamid 2026-07-22).

`RecurringTaskSeries` — one row per series. `promote_to_recurring()`
in app/services/recurring_tasks.py creates the row from an existing
Task; a cron tick generates future occurrences until end_condition
fires.

`RecurringTaskException` — dates the user explicitly asked to skip.

Frequency values (kept as strings for easy extensibility, matching
the TaskSchedule pattern):
  · DAILY
  · WEEKLY
  · MONTHLY
  · YEARLY
  · CUSTOM  (uses interval_count)

End conditions:
  · NEVER
  · AFTER_N   (stop after end_count occurrences)
  · END_DATE  (stop after end_date, inclusive)
"""
from datetime import datetime
from app import db


FREQ_DAILY = "DAILY"
FREQ_WEEKLY = "WEEKLY"
FREQ_MONTHLY = "MONTHLY"
FREQ_YEARLY = "YEARLY"
FREQ_CUSTOM = "CUSTOM"
FREQUENCIES = (FREQ_DAILY, FREQ_WEEKLY, FREQ_MONTHLY, FREQ_YEARLY, FREQ_CUSTOM)

END_NEVER = "NEVER"
END_AFTER_N = "AFTER_N"
END_ON_DATE = "END_DATE"
END_CONDITIONS = (END_NEVER, END_AFTER_N, END_ON_DATE)


class RecurringTaskSeries(db.Model):
    __tablename__ = "recurring_task_series"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"),
                           nullable=False, index=True)
    template_task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"),
                                  nullable=False)
    frequency = db.Column(db.String(20), nullable=False)
    interval_count = db.Column(db.Integer, default=1, nullable=False)
    end_condition = db.Column(db.String(20), nullable=False,
                              default=END_NEVER)
    end_count = db.Column(db.Integer, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    start_date = db.Column(db.Date, nullable=False)
    last_generated_date = db.Column(db.Date, nullable=True)
    generated_count = db.Column(db.Integer, default=0, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                           nullable=False)

    template_task = db.relationship("Task",
                                     foreign_keys=[template_task_id])
    exceptions = db.relationship(
        "RecurringTaskException", cascade="all, delete-orphan",
        backref="series",
    )


class RecurringTaskException(db.Model):
    __tablename__ = "recurring_task_exceptions"

    id = db.Column(db.Integer, primary_key=True)
    series_id = db.Column(db.Integer,
                          db.ForeignKey("recurring_task_series.id",
                                        ondelete="CASCADE"),
                          nullable=False, index=True)
    skip_date = db.Column(db.Date, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("series_id", "skip_date",
                             name="uq_recurring_task_exception"),
    )
