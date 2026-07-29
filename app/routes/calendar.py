"""Internal calendar view — lists upcoming meetings (Lead.next_meeting),
task deadlines, and project end-dates in one chronologically-sorted timeline.

This satisfies FR-39's literal requirement (system shows a calendar of meetings
+ deadlines). External-calendar integration (Google / Outlook) is explicitly
deferred to v1.5+ per the SRD's MVP-scope section.
"""
from datetime import date, datetime, timedelta
from flask import (
    Blueprint, render_template, g, request, redirect, url_for, flash,
    abort,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    Lead, Task, Project, TaskStatus, ProjectStatus,
    LeadActivity, LeadActivityType, CalendarEvent,
)
from app.services.permissions import get_user_role


def _parse_dt(raw):
    """Accept HTML5 datetime-local ("YYYY-MM-DDTHH:MM") or ISO."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
                 "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


bp = Blueprint("calendar", __name__)


@bp.route("/")
@login_required
def index():
    cid = g.active_company.id
    today = date.today()
    # Default: 30 days ahead
    window = int(request.args.get("days", "30"))
    horizon = today + timedelta(days=max(7, min(window, 180)))

    role = get_user_role(current_user.id, cid)

    # Meetings from leads — sales staff or above
    meetings = []
    if role in ("owner", "admin", "ceo", "sales_manager", "sales_rep"):
        q = Lead.query.filter(
            Lead.company_id == cid,
            Lead.next_meeting.isnot(None),
            Lead.next_meeting >= datetime.combine(today, datetime.min.time()),
            Lead.next_meeting < datetime.combine(horizon, datetime.min.time()),
        )
        if role == "sales_rep":
            q = q.filter(Lead.assigned_to_id == current_user.id)
        for lead in q.order_by(Lead.next_meeting).all():
            meetings.append({
                "when": lead.next_meeting,
                "kind": "meeting",
                "title": f"اجتماع مع {lead.client_name}",
                "subtitle": lead.service_needed,
                "link": f"/leads/{lead.id}",
            })

        # MARSOUD-CRM-CALENDAR (Abdelhamid 2026-07-13) — meetings + any
        # activity with a future follow_up_date logged via the CRM
        # activities panel were invisible here because they didn't
        # touch Lead.next_meeting. Read them directly from
        # LeadActivity so every upcoming touchpoint shows up. A MEETING
        # activity uses its own activity_date (that IS the meeting
        # time); other activity types surface via follow_up_date
        # (the "call me back on X" flag).
        _seen = {(m["when"], m["link"]) for m in meetings}
        start = datetime.combine(today, datetime.min.time())
        end = datetime.combine(horizon, datetime.min.time())
        aq = LeadActivity.query.join(Lead).filter(
            LeadActivity.company_id == cid,
            Lead.deleted_at.is_(None),
        )
        if role == "sales_rep":
            aq = aq.filter(Lead.assigned_to_id == current_user.id)

        for act in aq.all():
            # A MEETING activity's own activity_date is the event time.
            if (act.type == LeadActivityType.MEETING
                    and act.activity_date is not None
                    and start <= act.activity_date < end):
                key = (act.activity_date, f"/leads/{act.lead_id}")
                if key not in _seen:
                    _seen.add(key)
                    meetings.append({
                        "when": act.activity_date,
                        "kind": "meeting",
                        "title": (act.subject
                                    or f"اجتماع مع {act.lead.client_name}"),
                        "subtitle": act.lead.client_name,
                        "link": f"/leads/{act.lead_id}",
                    })
            # Any activity type: an explicit follow_up_date is a
            # follow-up event on the timeline.
            if (act.follow_up_date is not None
                    and start <= act.follow_up_date < end):
                key = (act.follow_up_date,
                       f"/leads/{act.lead_id}#activity-{act.id}")
                if key in _seen:
                    continue
                _seen.add(key)
                meetings.append({
                    "when": act.follow_up_date,
                    "kind": "follow_up",
                    "title": (f"{act.type.icon} متابعة: "
                                f"{act.subject or act.type.label_ar}"),
                    "subtitle": act.lead.client_name,
                    "link": f"/leads/{act.lead_id}",
                })

    # Task deadlines
    tasks = []
    if role in ("owner", "admin", "ceo", "project_manager", "team_member"):
        tq = Task.query.filter(
            Task.company_id == cid,
            Task.deadline.isnot(None),
            Task.deadline >= today, Task.deadline <= horizon,
            Task.status.notin_([TaskStatus.DONE, TaskStatus.BLOCKED]),
        )
        if role == "team_member":
            tq = tq.filter(Task.assigned_to_id == current_user.id)
        for t in tq.order_by(Task.deadline).all():
            # MARSOUD bug-fix: standalone tasks (project_id=None) used
            # to crash the calendar via `t.project.name` (AttributeError →
            # 500 on /calendar/). Guard both the project name and the
            # priority label (priority is nullable on legacy rows).
            project_label = t.project.name if t.project else "مهمة مستقلة"
            priority_label = t.priority.label_ar if t.priority else "—"
            tasks.append({
                "when": datetime.combine(t.deadline, datetime.min.time()),
                "kind": "task_deadline",
                "title": f"Deadline: {t.title}",
                "subtitle": f"{project_label} · {priority_label}",
                "link": f"/tasks/{t.id}",
            })

    # Project end dates
    projects = []
    if role in ("owner", "admin", "ceo", "project_manager"):
        pq = Project.query.filter(
            Project.company_id == cid,
            Project.end_date >= today, Project.end_date <= horizon,
            Project.status.notin_([ProjectStatus.CLOSED]),
        )
        if role == "project_manager":
            pq = pq.filter(Project.manager_id == current_user.id)
        for p in pq.order_by(Project.end_date).all():
            projects.append({
                "when": datetime.combine(p.end_date, datetime.min.time()),
                "kind": "project_end",
                "title": f"تسليم متوقع: {p.name}",
                "subtitle": p.type,
                "link": f"/projects/{p.id}",
            })

    # MARSOUD-CALENDAR-MANUAL-EVENTS (Abdelhamid 2026-07-29) — user-
    # added events (visible to everyone in the company, regardless
    # of role). Same window as the derived events.
    manual_events = []
    start_dt = datetime.combine(today, datetime.min.time())
    end_dt = datetime.combine(horizon, datetime.min.time())
    mq = CalendarEvent.query.filter(
        CalendarEvent.company_id == cid,
        CalendarEvent.is_deleted.is_(False),
        CalendarEvent.starts_at >= start_dt,
        CalendarEvent.starts_at < end_dt,
    ).order_by(CalendarEvent.starts_at)
    for e in mq.all():
        manual_events.append({
            "when": e.starts_at,
            "kind": "manual",
            "title": e.title,
            "subtitle": (e.location or e.description or "")[:120],
            "link": None,
            "event_id": e.id,
        })

    events = sorted(meetings + tasks + projects + manual_events,
                    key=lambda e: e["when"])
    # Group by date for the timeline render
    by_day = {}
    for e in events:
        d = e["when"].date()
        by_day.setdefault(d, []).append(e)

    return render_template("calendar/index.html",
                           by_day=by_day, today=today,
                           horizon=horizon, window=window,
                           total=len(events))


# ─── MARSOUD-CALENDAR-MANUAL-EVENTS (Abdelhamid 2026-07-29) ───
@bp.route("/events", methods=["POST"])
@login_required
def create_event():
    cid = g.active_company.id
    title = (request.form.get("title") or "").strip()
    starts_raw = request.form.get("starts_at")
    if not title or not starts_raw:
        flash("لازم تدخل عنوان وتاريخ بداية.", "error")
        return redirect(url_for("calendar.index"))
    starts_at = _parse_dt(starts_raw)
    if not starts_at:
        flash("تنسيق تاريخ البداية غير صحيح.", "error")
        return redirect(url_for("calendar.index"))
    ends_at = _parse_dt(request.form.get("ends_at"))
    if ends_at and ends_at < starts_at:
        flash("تاريخ النهاية لازم يكون بعد البداية.", "error")
        return redirect(url_for("calendar.index"))
    reminder_raw = request.form.get("reminder_minutes_before")
    reminder = None
    if reminder_raw:
        try:
            reminder = int(reminder_raw)
            if reminder < 0:
                reminder = None
        except ValueError:
            reminder = None
    ev = CalendarEvent(
        company_id=cid,
        created_by_id=current_user.id,
        title=title[:255],
        description=(request.form.get("description") or "").strip()
                     or None,
        starts_at=starts_at,
        ends_at=ends_at,
        location=(request.form.get("location") or "").strip()[:500]
                  or None,
        reminder_minutes_before=reminder,
    )
    db.session.add(ev)
    db.session.commit()
    flash(f"تم إضافة الحدث: {title}", "success")
    return redirect(url_for("calendar.index"))


@bp.route("/events/<int:event_id>/edit", methods=["POST"])
@login_required
def edit_event(event_id):
    cid = g.active_company.id
    ev = CalendarEvent.query.filter_by(
        id=event_id, company_id=cid, is_deleted=False).first()
    if not ev:
        abort(404)
    title = (request.form.get("title") or "").strip()
    starts_at = _parse_dt(request.form.get("starts_at"))
    if not title or not starts_at:
        flash("لازم تدخل عنوان وتاريخ بداية.", "error")
        return redirect(url_for("calendar.index"))
    ends_at = _parse_dt(request.form.get("ends_at"))
    if ends_at and ends_at < starts_at:
        flash("تاريخ النهاية لازم يكون بعد البداية.", "error")
        return redirect(url_for("calendar.index"))
    reminder_raw = request.form.get("reminder_minutes_before")
    reminder = None
    if reminder_raw:
        try:
            reminder = int(reminder_raw)
            if reminder < 0:
                reminder = None
        except ValueError:
            reminder = None
    ev.title = title[:255]
    ev.description = ((request.form.get("description") or "").strip()
                       or None)
    ev.starts_at = starts_at
    ev.ends_at = ends_at
    ev.location = ((request.form.get("location") or "").strip()[:500]
                    or None)
    ev.reminder_minutes_before = reminder
    db.session.commit()
    flash("تم تحديث الحدث.", "success")
    return redirect(url_for("calendar.index"))


@bp.route("/events/<int:event_id>/delete", methods=["POST"])
@login_required
def delete_event(event_id):
    cid = g.active_company.id
    ev = CalendarEvent.query.filter_by(
        id=event_id, company_id=cid, is_deleted=False).first()
    if not ev:
        abort(404)
    ev.is_deleted = True
    db.session.commit()
    flash("تم حذف الحدث.", "success")
    return redirect(url_for("calendar.index"))
