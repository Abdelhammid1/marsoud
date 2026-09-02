"""MARSOUD-MOBILE-TKT-01 (2026-08-18) — JSON APIs for the three
mobile modules the ticket lists that had no /api/v1 coverage
yet: CRM Leads, Meetings, and Schedules.

Three thin blueprints mounted under /api/v1/my/{leads,meetings,
schedules}. Each blueprint installs the shared bearer-token
gate via `install_api_guard(bp)` — identical pattern to
`api_v1_notifications.py` and `api_v1_me.py`. All queries are
scoped to (current_user, g.active_company); the mobile app only
ever sees the caller's own data (or company data the caller has
permission to read).

Business logic reuses:
  · `app/services/crm.py::change_lead_status` for status
    transitions (event log + notification fanout already
    handled there).
  · `LeadActivity` model directly for activity inserts (no
    service helper yet — thin wrapper here).
  · `CalendarEvent` model for manual meeting creation.
  · `TaskSchedule` model for recurring-task schedules.

None of the routes require a new permission — visibility rules
follow the existing web patterns: leads are visible to their
assigned rep + everyone with `leads.view`; meetings/schedules
are per-user by default.
"""
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, g
from flask_login import current_user

from app import db
from app.models import (
    Lead, LeadStatus, LeadStatusEvent, CalendarEvent, User,
)
from app.models.crm_expansion import LeadActivity, LeadActivityType
from app.models.task_schedule import TaskSchedule
from app.services.api_guard import install_api_guard


# ─── Helpers ──────────────────────────────────────────────────────────
def _err(msg, status=400, extra=None):
    body = {"error": msg}
    if extra:
        body.update(extra)
    r = jsonify(body)
    r.status_code = status
    return r


def _body():
    return request.get_json(silent=True) or request.form or {}


def _parse_dt(raw):
    """ISO-8601 or 'YYYY-MM-DD HH:MM' → datetime, or None."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # Also accept "2026-08-18T12:34:56Z"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _lead_brief(lead):
    return {
        "id": lead.id,
        "number": lead.number,
        "client_name": lead.client_name,
        "email": lead.email,
        "phone": lead.phone,
        "service_needed": lead.service_needed,
        "status": lead.status.value if lead.status else None,
        "status_label_ar":
            lead.status.label_ar if lead.status else None,
        "status_badge":
            lead.status.badge_class if lead.status else None,
        "assigned_to_id": lead.assigned_to_id,
        "next_meeting":
            lead.next_meeting.isoformat()
            if lead.next_meeting else None,
        "expected_value":
            float(lead.expected_value)
            if lead.expected_value is not None else None,
        "created_at":
            lead.created_at.isoformat()
            if lead.created_at else None,
    }


def _lead_activity_brief(a):
    return {
        "id": a.id,
        "type": a.type.value if a.type else None,
        "type_label_ar":
            a.type.label_ar if a.type else None,
        "type_icon":
            a.type.icon if a.type else None,
        "subject": a.subject,
        "body": a.body,
        "activity_date":
            a.activity_date.isoformat()
            if a.activity_date else None,
        "follow_up_date":
            a.follow_up_date.isoformat()
            if a.follow_up_date else None,
    }


def _lead_event_brief(ev):
    return {
        "id": ev.id,
        "from_status":
            ev.from_status.value if ev.from_status else None,
        "to_status":
            ev.to_status.value if ev.to_status else None,
        "to_status_label_ar":
            ev.to_status.label_ar if ev.to_status else None,
        "note": ev.note,
        "created_at":
            ev.created_at.isoformat() if ev.created_at else None,
    }


# ══════════════════════════════════════════════════════════════════════
#  A. Leads  — /api/v1/my/leads
# ══════════════════════════════════════════════════════════════════════
leads_bp = Blueprint("api_v1_leads", __name__)
install_api_guard(leads_bp)


@leads_bp.route("/stages", methods=["GET"])
def leads_stages():
    """Return every LeadStatus value with its Arabic label + badge
    class so the mobile UI can render filter chips + a status
    picker without hard-coding the enum."""
    return jsonify({
        "stages": [
            {
                "code": s.value,
                "label_ar": s.label_ar,
                "badge_class": s.badge_class,
            }
            for s in LeadStatus
        ]
    })


@leads_bp.route("", methods=["GET"])
def leads_list():
    """List leads visible to me. Scope:
      · Anyone assigned as `assigned_to_id = me`.
      · Users with `leads.view_all` (owner/admin/sales_manager)
        see every non-deleted lead in the active company.
    """
    from app.services.permissions import has_permission
    q = Lead.query.filter(
        Lead.company_id == g.active_company.id,
        Lead.deleted_at.is_(None),
    )
    if not has_permission("leads.view_all"):
        q = q.filter(Lead.assigned_to_id == current_user.id)
    # Optional status filter.
    status_arg = (request.args.get("status") or "").strip().upper()
    if status_arg:
        try:
            q = q.filter(Lead.status == LeadStatus[status_arg])
        except KeyError:
            pass
    try:
        limit = min(200, max(1, int(request.args.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    rows = q.order_by(Lead.created_at.desc()).limit(limit).all()
    return jsonify({
        "count": len(rows),
        "leads": [_lead_brief(l) for l in rows],
    })


@leads_bp.route("/<int:lead_id>", methods=["GET"])
def lead_detail(lead_id):
    lead = _get_lead_or_404(lead_id)
    if not isinstance(lead, Lead):   # error Response — re-return it
        return lead
    activities = (LeadActivity.query
                   .filter_by(lead_id=lead.id)
                   .order_by(LeadActivity.activity_date.desc())
                   .limit(100).all())
    history = (LeadStatusEvent.query
                .filter_by(lead_id=lead.id)
                .order_by(LeadStatusEvent.created_at.desc())
                .limit(100).all())
    body = _lead_brief(lead)
    body.update({
        "notes": lead.notes,
        "meeting_notes": lead.meeting_notes,
        "request_description": lead.request_description,
        "sales_action_required": lead.sales_action_required,
        "activities": [_lead_activity_brief(a) for a in activities],
        "history": [_lead_event_brief(e) for e in history],
    })
    return jsonify({"lead": body})


@leads_bp.route("/<int:lead_id>/status", methods=["POST"])
def lead_change_status(lead_id):
    """Move a lead to a new status. Reuses the canonical service
    helper so the LeadStatusEvent row + notification fanout stay
    consistent with the web flow."""
    lead = _get_lead_or_404(lead_id)
    if not isinstance(lead, Lead):   # error Response — re-return it
        return lead
    body = _body()
    new_status = (body.get("new_status") or "").strip().upper()
    note = (body.get("note") or "").strip() or None
    lost_reason = (body.get("lost_reason") or "").strip() or None
    if not new_status:
        return _err("new_status required", 400)
    from app.services.crm import change_lead_status, CRMError
    try:
        change_lead_status(
            lead, new_status,
            changed_by_id=current_user.id,
            note=note,
            lost_reason=lost_reason,
        )
    except CRMError as e:
        return _err(str(e), 400)
    return jsonify({"ok": True, "lead": _lead_brief(lead)}), 200


@leads_bp.route("/<int:lead_id>/activities", methods=["GET"])
def lead_activities(lead_id):
    lead = _get_lead_or_404(lead_id)
    if not isinstance(lead, Lead):   # error Response — re-return it
        return lead
    rows = (LeadActivity.query
             .filter_by(lead_id=lead.id)
             .order_by(LeadActivity.activity_date.desc())
             .limit(200).all())
    return jsonify({
        "count": len(rows),
        "activities": [_lead_activity_brief(a) for a in rows],
    })


@leads_bp.route("/<int:lead_id>/activities", methods=["POST"])
def lead_add_activity(lead_id):
    lead = _get_lead_or_404(lead_id)
    if not isinstance(lead, Lead):   # error Response — re-return it
        return lead
    body = _body()
    type_raw = (body.get("type") or "NOTE").strip().upper()
    try:
        atype = LeadActivityType[type_raw]
    except KeyError:
        return _err("invalid_type", 400,
                     {"allowed":
                         [t.value for t in LeadActivityType]})
    subject = (body.get("subject") or "").strip()[:255] or None
    body_text = (body.get("body") or body.get("details") or
                  "").strip() or None
    when = _parse_dt(body.get("activity_date")) or datetime.utcnow()
    follow_up = _parse_dt(body.get("follow_up_date"))
    row = LeadActivity(
        company_id=lead.company_id,
        lead_id=lead.id,
        type=atype,
        subject=subject,
        body=body_text,
        activity_date=when,
        follow_up_date=follow_up,
        created_by_id=current_user.id,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify({
        "ok": True,
        "activity": _lead_activity_brief(row),
    }), 201


def _get_lead_or_404(lead_id):
    """Fetch a lead scoped to the caller. Returns the Lead OR an
    error Response (which the caller re-returns).

    MARSOUD-MOBILE-LEAD-GUARD (2026-08-24) — this used to say "error
    response tuple", and every caller guarded with
    `isinstance(lead, tuple)`. `_err()` builds a Response via jsonify(),
    never a tuple, so that guard was ALWAYS False: a forbidden or
    missing lead fell straight through to `lead.id` and raised
    AttributeError: 'Response' object has no attribute 'id' -> HTTP 500
    on four of the five lead endpoints, for both the 403 and the 404
    path. Callers now test for the positive type instead."""
    from app.services.permissions import has_permission
    lead = db.session.get(Lead, lead_id)
    if (not lead
            or lead.company_id != g.active_company.id
            or lead.deleted_at is not None):
        return _err("lead_not_found", 404)
    # Visibility: assigned rep OR anyone with leads.view_all.
    if (lead.assigned_to_id != current_user.id
            and not has_permission("leads.view_all")):
        return _err("forbidden", 403)
    return lead


# ══════════════════════════════════════════════════════════════════════
#  B. Meetings — /api/v1/my/meetings
# ══════════════════════════════════════════════════════════════════════
meetings_bp = Blueprint("api_v1_meetings", __name__)
install_api_guard(meetings_bp)


def _calendar_event_brief(ce):
    return {
        "id": ce.id,
        "source": "calendar_event",
        "title": ce.title,
        "description": ce.description,
        "starts_at":
            ce.starts_at.isoformat() if ce.starts_at else None,
        "ends_at":
            ce.ends_at.isoformat() if ce.ends_at else None,
        "location": ce.location,
        "lead_id": None,
    }


def _lead_meeting_brief(activity):
    """LeadActivity where type=MEETING is a "meeting". Return it in
    the same shape as _calendar_event_brief so the mobile app can
    render one merged list."""
    return {
        "id": activity.id,
        "source": "lead_activity",
        "title": activity.subject or "اجتماع مع عميل محتمل",
        "description": activity.body,
        "starts_at":
            activity.activity_date.isoformat()
            if activity.activity_date else None,
        "ends_at": None,
        "location": None,
        "lead_id": activity.lead_id,
    }


@meetings_bp.route("", methods=["GET"])
def meetings_list():
    """Merged meetings source:
      · CalendarEvent rows created by me OR company-wide
        (no per-attendee schema yet — see out-of-scope in plan).
      · LeadActivity rows with type=MEETING on leads assigned
        to me.
    Query params:
      · ?upcoming_only=1   (default 1 — hide past events)
      · ?days=30           (window; default 30)
    """
    upcoming_only = (request.args.get("upcoming_only", "1")
                     in ("1", "true", "yes"))
    try:
        days = min(365, max(1, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    now = datetime.utcnow()
    horizon = now + timedelta(days=days)

    # Calendar events — my creations or company-wide (creator_id
    # NULL means it's not user-tagged; safest to include).
    ce_q = CalendarEvent.query.filter(
        CalendarEvent.company_id == g.active_company.id,
        CalendarEvent.is_deleted.is_(False),
    )
    if upcoming_only:
        ce_q = ce_q.filter(CalendarEvent.starts_at >= now)
    ce_q = ce_q.filter(CalendarEvent.starts_at <= horizon)
    ce_rows = ce_q.order_by(CalendarEvent.starts_at.asc()).all()

    # Lead activity meetings for leads assigned to me.
    la_q = (LeadActivity.query
             .join(Lead, LeadActivity.lead_id == Lead.id)
             .filter(LeadActivity.company_id == g.active_company.id)
             .filter(LeadActivity.type == LeadActivityType.MEETING)
             .filter(Lead.assigned_to_id == current_user.id)
             .filter(Lead.deleted_at.is_(None)))
    if upcoming_only:
        la_q = la_q.filter(LeadActivity.activity_date >= now)
    la_q = la_q.filter(LeadActivity.activity_date <= horizon)
    la_rows = la_q.order_by(LeadActivity.activity_date.asc()).all()

    merged = [_calendar_event_brief(ce) for ce in ce_rows] \
             + [_lead_meeting_brief(la) for la in la_rows]
    merged.sort(key=lambda m: m["starts_at"] or "")
    return jsonify({
        "count": len(merged),
        "meetings": merged,
    })


@meetings_bp.route("", methods=["POST"])
def meetings_create():
    """Create a meeting. If `lead_id` is present, insert a
    LeadActivity of type=MEETING; else create a CalendarEvent."""
    body = _body()
    title = (body.get("title") or "").strip()
    starts_at = _parse_dt(body.get("starts_at"))
    if not title:
        return _err("title_required", 400)
    if not starts_at:
        return _err("starts_at_required", 400)
    ends_at = _parse_dt(body.get("ends_at"))
    location = (body.get("location") or "").strip() or None
    notes = (body.get("notes") or body.get("description")
              or "").strip() or None
    lead_id = body.get("lead_id")

    if lead_id:
        # Attach to a lead as a MEETING activity — reuses the
        # existing model so the meeting shows up in the lead
        # timeline AND on the meetings screen.
        try:
            lid = int(lead_id)
        except (TypeError, ValueError):
            return _err("invalid_lead_id", 400)
        lead = _get_lead_or_404(lid)
        if not isinstance(lead, Lead):   # error Response — re-return it
            return lead
        row = LeadActivity(
            company_id=lead.company_id,
            lead_id=lead.id,
            type=LeadActivityType.MEETING,
            subject=title,
            body=notes,
            activity_date=starts_at,
            created_by_id=current_user.id,
        )
        db.session.add(row)
        db.session.commit()
        return jsonify({
            "ok": True,
            "meeting": _lead_meeting_brief(row),
        }), 201

    # Free-standing calendar event.
    ce = CalendarEvent(
        company_id=g.active_company.id,
        created_by_id=current_user.id,
        title=title,
        description=notes,
        starts_at=starts_at,
        ends_at=ends_at,
        location=location,
    )
    db.session.add(ce)
    db.session.commit()
    return jsonify({
        "ok": True,
        "meeting": _calendar_event_brief(ce),
    }), 201


# ══════════════════════════════════════════════════════════════════════
#  C. Schedules — /api/v1/my/schedules
# ══════════════════════════════════════════════════════════════════════
schedules_bp = Blueprint("api_v1_schedules", __name__)
install_api_guard(schedules_bp)


def _schedule_brief(s):
    return {
        "id": s.id,
        "title": s.title,
        "description": s.description,
        "priority": s.priority.value
                    if hasattr(s.priority, "value") else s.priority,
        "recurrence": s.recurrence,
        "start_date":
            s.start_date.isoformat() if s.start_date else None,
        "end_date":
            s.end_date.isoformat() if s.end_date else None,
        "active": s.active,
        "generated_count": s.generated_count,
        "last_generated_date":
            s.last_generated_date.isoformat()
            if s.last_generated_date else None,
        "project_id": s.project_id,
        "assigned_to_id": s.assigned_to_id,
    }


@schedules_bp.route("", methods=["GET"])
def schedules_list():
    """List task-schedules scoped to me: either primary assignee OR
    a member of task_schedule_assignees M2M."""
    from app.models.task_schedule import task_schedule_assignees
    my_ids = {
        row.task_schedule_id
        for row in db.session.execute(
            task_schedule_assignees.select().where(
                task_schedule_assignees.c.user_id == current_user.id
            )
        ).all()
    }
    # MARSOUD-MOBILE-SCHEDULES-FIX-01 (2026-09-03) — was
    # `TaskSchedule.id.in_(my_ids) if my_ids else False` inside
    # db.or_(). The literal Python `False` isn't a valid SQL
    # expression on SQLAlchemy 2.x → the whole query 500'd, mobile
    # showed "internal error" on /جدولي. Now build the OR clauses
    # list conditionally so we only pass real SQL predicates.
    or_clauses = [TaskSchedule.assigned_to_id == current_user.id]
    if my_ids:
        or_clauses.append(TaskSchedule.id.in_(my_ids))
    q = TaskSchedule.query.filter(
        TaskSchedule.company_id == g.active_company.id,
    ).filter(db.or_(*or_clauses))
    rows = q.order_by(TaskSchedule.start_date.desc()).limit(200).all()
    return jsonify({
        "count": len(rows),
        "schedules": [_schedule_brief(s) for s in rows],
    })


@schedules_bp.route("/<int:schedule_id>", methods=["GET"])
def schedule_detail(schedule_id):
    from app.models.task_schedule import task_schedule_assignees
    s = db.session.get(TaskSchedule, schedule_id)
    if not s or s.company_id != g.active_company.id:
        return _err("schedule_not_found", 404)
    # Visibility guard.
    is_assigned = s.assigned_to_id == current_user.id
    is_member = db.session.execute(
        task_schedule_assignees.select().where(
            (task_schedule_assignees.c.task_schedule_id == s.id) &
            (task_schedule_assignees.c.user_id == current_user.id)
        )
    ).first() is not None
    from app.services.permissions import has_permission
    if not (is_assigned or is_member or has_permission("tasks.view_all")):
        return _err("forbidden", 403)
    return jsonify({"schedule": _schedule_brief(s)})
