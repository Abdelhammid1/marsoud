from app.services.url_ids import encode_id
"""Internal calendar view — lists upcoming meetings (Lead.next_meeting),
task deadlines, and project end-dates in one chronologically-sorted timeline.

This satisfies FR-39's literal requirement (system shows a calendar of meetings
+ deadlines). External-calendar integration (Google / Outlook) is explicitly
deferred to v1.5+ per the SRD's MVP-scope section.
"""
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from flask import (
    Blueprint, render_template, g, request, redirect, url_for, flash,
    abort, Response,
)
from flask_login import login_required, current_user

from app import db
from app.models import (
    Lead, Task, Project, TaskStatus, ProjectStatus,
    LeadActivity, LeadActivityType, CalendarEvent,
    CalendarIntegration, User,
)
from app.models.user import user_companies
from flask import jsonify
from app.services.calendar_sync import (
    build_google_calendar_url, build_ics_content,
    sync_company_calendar, upsert_booking_lead, _resolve_assignee_id,
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
                "link": f"/leads/{encode_id(lead.id)}",
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
                key = (act.activity_date, f"/leads/{encode_id(act.lead_id)}")
                if key not in _seen:
                    _seen.add(key)
                    meetings.append({
                        "when": act.activity_date,
                        "kind": "meeting",
                        "title": (act.subject
                                    or f"اجتماع مع {act.lead.client_name}"),
                        "subtitle": act.lead.client_name,
                        "link": f"/leads/{encode_id(act.lead_id)}",
                    })
            # Any activity type: an explicit follow_up_date is a
            # follow-up event on the timeline.
            if (act.follow_up_date is not None
                    and start <= act.follow_up_date < end):
                key = (act.follow_up_date,
                       f"/leads/{encode_id(act.lead_id)}#activity-{act.id}")
                if key in _seen:
                    continue
                _seen.add(key)
                meetings.append({
                    "when": act.follow_up_date,
                    "kind": "follow_up",
                    "title": (f"{act.type.icon} متابعة: "
                                f"{act.subject or act.type.label_ar}"),
                    "subtitle": act.lead.client_name,
                    "link": f"/leads/{encode_id(act.lead_id)}",
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
                "link": f"/tasks/{encode_id(t.id)}",
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
                "link": f"/projects/{encode_id(p.id)}",
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
    base_root = request.url_root.rstrip("/")
    for e in events:
        full_link = (base_root + e["link"]) if e.get("link") else (base_root + "/calendar/")
        det_txt = ((e.get("subtitle") or "") + f"\n{full_link}").strip()
        e["google_url"] = build_google_calendar_url(
            e["title"], e["when"], details=det_txt
        )
        e["ics_url"] = url_for(
            "calendar.download_ics",
            title=e["title"],
            starts_at=e["when"].strftime("%Y-%m-%dT%H:%M"),
            details=e.get("subtitle") or "",
            url=full_link,
        )
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


@bp.route("/events/<hid:event_id>/edit", methods=["POST"])
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


@bp.route("/events/<hid:event_id>/delete", methods=["POST"])
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


# ─── MARSOUD-P120: Outbound ICS, Feed & Integration Settings ───────────
@bp.route("/ics")
@login_required
def download_ics():
    title = (request.args.get("title") or "موعد مرصود").strip()[:200]
    starts_at = _parse_dt(request.args.get("starts_at")) or datetime.now()
    ends_at = _parse_dt(request.args.get("ends_at"))
    details = (request.args.get("details") or "").strip()[:1000]
    location = (request.args.get("location") or "").strip()[:300]
    ev_url = (request.args.get("url") or "").strip()[:500]
    ics = build_ics_content([{
        "title": title,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "description": details,
        "location": location,
        "url": ev_url,
    }])
    return Response(
        ics,
        mimetype="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="marsoud-event.ics"'},
    )


@bp.route("/feed/<token>.ics")
def subscription_feed(token):
    cfg = CalendarIntegration.query.filter_by(feed_token=token).first()
    if not cfg:
        abort(404)
    cid = cfg.company_id
    today = date.today()
    start_dt = datetime.combine(today - timedelta(days=7), datetime.min.time())
    end_dt = datetime.combine(today + timedelta(days=90), datetime.min.time())
    items = []
    for lead in Lead.query.filter(
        Lead.company_id == cid,
        Lead.deleted_at.is_(None),
        Lead.next_meeting.isnot(None),
        Lead.next_meeting >= start_dt,
        Lead.next_meeting < end_dt,
    ).all():
        items.append({
            "uid": f"marsoud-lead-{lead.id}@marsoud.com",
            "title": f"اجتماع مع {lead.client_name}",
            "starts_at": lead.next_meeting,
            "description": lead.service_needed or "",
        })
    for t in Task.query.filter(
        Task.company_id == cid,
        Task.archived_at.is_(None),
        Task.deadline.isnot(None),
        Task.deadline >= today - timedelta(days=7),
        Task.deadline <= today + timedelta(days=90),
        Task.status.notin_([TaskStatus.DONE, TaskStatus.BLOCKED]),
    ).all():
        items.append({
            "uid": f"marsoud-task-{t.id}@marsoud.com",
            "title": f"مهمة: {t.title}",
            "starts_at": t.deadline,
            "description": t.notes or "",
        })
    for ev in CalendarEvent.query.filter(
        CalendarEvent.company_id == cid,
        CalendarEvent.is_deleted.is_(False),
        CalendarEvent.starts_at >= start_dt,
        CalendarEvent.starts_at < end_dt,
    ).all():
        items.append({
            "uid": f"marsoud-ev-{ev.id}@marsoud.com",
            "title": ev.title,
            "starts_at": ev.starts_at,
            "ends_at": ev.ends_at,
            "description": ev.description or "",
            "location": ev.location or "",
        })
    ics = build_ics_content(items, cal_name="Marsoud — المواعيد والمهام")
    return Response(ics, mimetype="text/calendar; charset=utf-8")


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def integration_settings():
    cid = g.active_company.id
    role = get_user_role(current_user.id, cid)
    if role not in ("owner", "admin", "ceo", "sales_manager"):
        abort(403)
    cfg = CalendarIntegration.get_or_create(cid)
    if request.method == "POST":
        url_val = (request.form.get("google_ical_url") or "").strip()[:1000]
        if url_val and not (url_val.startswith("https://") or url_val.startswith("http://")):
            flash("رابط الـ iCal لازم يبدأ بـ https://", "error")
            return redirect(url_for("calendar.integration_settings"))
        cfg.google_ical_url = url_val or None
        raw_assg = request.form.get("default_assignee_id") or ""
        cfg.default_assignee_id = int(raw_assg) if raw_assg.isdigit() else None
        cfg.is_active = request.form.get("is_active") == "1"
        db.session.commit()
        flash("تم حفظ إعدادات ربط Google Bookings والتقويم.", "success")
        return redirect(url_for("calendar.integration_settings"))

    member_ids = [
        r[0] for r in db.session.execute(
            db.select(user_companies.c.user_id).where(user_companies.c.company_id == cid)
        ).fetchall()
    ]
    users = User.query.filter(User.id.in_(member_ids), User.is_active.is_(True)).order_by(User.full_name).all() if member_ids else []
    base = request.url_root.rstrip("/")
    feed_url = base + url_for("calendar.subscription_feed", token=cfg.feed_token)
    webcal_url = feed_url.replace("https://", "webcal://").replace("http://", "webcal://")
    webhook_url = f"{base}/calendar/webhook/{cfg.webhook_secret}"
    return render_template(
        "calendar/settings.html",
        cfg=cfg,
        users=users,
        feed_url=feed_url,
        webcal_url=webcal_url,
        webhook_url=webhook_url,
    )


@bp.route("/settings/sync-now", methods=["POST"])
@login_required
def sync_now():
    cid = g.active_company.id
    role = get_user_role(current_user.id, cid)
    if role not in ("owner", "admin", "ceo", "sales_manager"):
        abort(403)
    cfg = CalendarIntegration.get_or_create(cid)
    if not cfg.google_ical_url:
        flash("من فضلك أدخل رابط Google Calendar Secret iCal URL واحفظ الإعدادات أولاً.", "error")
        return redirect(url_for("calendar.integration_settings"))
    res = sync_company_calendar(cfg)
    if res.get("error"):
        flash(f"تعذّرت المزامنة: {res['error']}", "error")
    else:
        flash(
            f"تمت المزامنة بنجاح: {res.get('created', 0)} حجز جديد، {res.get('updated', 0)} محدّث.",
            "success",
        )
    return redirect(url_for("calendar.integration_settings"))


@bp.route("/webhook/<secret>", methods=["POST"])
def booking_webhook(secret):
    cfg = CalendarIntegration.query.filter_by(webhook_secret=secret).first()
    if not cfg:
        abort(404)
    payload = request.get_json(silent=True) or request.form.to_dict() or {}
    uid = str(payload.get("uid") or payload.get("booking_id") or payload.get("event_id") or "").strip()
    starts_raw = str(payload.get("starts_at") or payload.get("start_time") or payload.get("start") or "").strip()
    starts_at = _parse_dt(starts_raw[:19].replace("Z", "")) if starts_raw else None
    if not starts_at:
        return jsonify({"ok": False, "error": "missing_or_invalid_starts_at"}), 400
    if not uid:
        uid = f"wh-{starts_at.strftime('%Y%m%d%H%M')}-{(payload.get('email') or payload.get('phone') or 'guest')}"
    ends_raw = str(payload.get("ends_at") or payload.get("end_time") or "").strip()
    ends_at = _parse_dt(ends_raw[:19].replace("Z", "")) if ends_raw else None
    assignee_id = _resolve_assignee_id(cfg)
    status = upsert_booking_lead(
        company_id=cfg.company_id,
        uid=uid[:200],
        starts_at=starts_at,
        ends_at=ends_at,
        client_name=str(payload.get("client_name") or payload.get("name") or "عميل حجز").strip(),
        email=str(payload.get("email") or "").strip() or None,
        phone=str(payload.get("phone") or "غير محدد").strip(),
        summary=str(payload.get("title") or payload.get("service") or "حجز موعد").strip(),
        description=str(payload.get("notes") or payload.get("description") or "").strip(),
        location=str(payload.get("location") or "").strip(),
        assignee_id=assignee_id,
        cancelled=bool(payload.get("cancelled")),
    )
    if status == "created":
        cfg.synced_count = (cfg.synced_count or 0) + 1
    cfg.last_synced_at = datetime.utcnow()
    cfg.last_sync_status = "ok"
    db.session.commit()
    return jsonify({"ok": True, "result": status})
