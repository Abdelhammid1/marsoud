"""Internal calendar view — lists upcoming meetings (Lead.next_meeting),
task deadlines, and project end-dates in one chronologically-sorted timeline.

This satisfies FR-39's literal requirement (system shows a calendar of meetings
+ deadlines). External-calendar integration (Google / Outlook) is explicitly
deferred to v1.5+ per the SRD's MVP-scope section.
"""
from datetime import date, datetime, timedelta
from flask import Blueprint, render_template, g, request
from flask_login import login_required, current_user

from app import db
from app.models import Lead, Task, Project, TaskStatus, ProjectStatus
from app.services.permissions import get_user_role


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
            tasks.append({
                "when": datetime.combine(t.deadline, datetime.min.time()),
                "kind": "task_deadline",
                "title": f"Deadline: {t.title}",
                "subtitle": f"{t.project.name} · {t.priority.label_ar}",
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

    events = sorted(meetings + tasks + projects, key=lambda e: e["when"])
    # Group by date for the timeline render
    by_day = {}
    for e in events:
        d = e["when"].date()
        by_day.setdefault(d, []).append(e)

    return render_template("calendar/index.html",
                           by_day=by_day, today=today,
                           horizon=horizon, window=window,
                           total=len(events))
