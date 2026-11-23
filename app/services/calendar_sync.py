"""MARSOUD-P120/P121 — Outbound Google/Apple Calendar & Inbound Google Bookings Sync."""
from __future__ import annotations
import re
from datetime import datetime, date, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app import db
from app.models import (
    Lead, LeadStatus, LeadStatusEvent, CalendarEvent, CalendarIntegration, User,
)
from app.models.user import user_companies
from app.services.numbering import next_number


def _to_dt_range(starts_at, ends_at=None, default_minutes: int = 60):
    if isinstance(starts_at, date) and not isinstance(starts_at, datetime):
        s = datetime.combine(starts_at, datetime.min.time()).replace(hour=9, minute=0)
    else:
        s = starts_at
    if ends_at:
        if isinstance(ends_at, date) and not isinstance(ends_at, datetime):
            e = datetime.combine(ends_at, datetime.min.time()).replace(hour=10, minute=0)
        else:
            e = ends_at
    else:
        e = s + timedelta(minutes=default_minutes)
    return s, e


def build_google_calendar_url(title: str, starts_at, ends_at=None,
                              details: str = "", location: str = "") -> str:
    s, e = _to_dt_range(starts_at, ends_at)
    dates_str = f"{s.strftime('%Y%m%dT%H%M%S')}/{e.strftime('%Y%m%dT%H%M%S')}"
    params = {
        "action": "TEMPLATE",
        "text": (title or "موعد مرصود").strip(),
        "dates": dates_str,
    }
    if details:
        params["details"] = details.strip()
    if location:
        params["location"] = location.strip()
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def _ics_escape(val: str) -> str:
    return (
        (val or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def build_ics_content(events: list[dict], cal_name: str = "Marsoud Calendar") -> str:
    now_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Marsoud ERP//Calendar Sync//AR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(cal_name)}",
    ]
    for ev in events:
        s, e = _to_dt_range(ev["starts_at"], ev.get("ends_at"))
        uid = ev.get("uid") or f"marsoud-{abs(hash((ev.get('title'), s.isoformat())))}@marsoud.com"
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_stamp}",
            f"DTSTART:{s.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND:{e.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:{_ics_escape(ev.get('title') or 'موعد مرصود')}",
        ])
        if ev.get("description"):
            lines.append(f"DESCRIPTION:{_ics_escape(ev['description'])}")
        if ev.get("location"):
            lines.append(f"LOCATION:{_ics_escape(ev['location'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ─── Inbound Google Bookings (ICS + Webhook) ─────────────────────────
def _unfold_ics(raw: str) -> list[str]:
    unfolded = re.sub(r"\r?\n[ \t]", "", raw or "")
    return [ln.strip() for ln in unfolded.splitlines() if ln.strip()]


def _unescape_ics(val: str) -> str:
    return (
        (val or "")
        .replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_ics_dt(val: str, tz_offset_hours: int = 3) -> datetime | None:
    if not val:
        return None
    v = val.strip()
    is_utc = v.endswith("Z")
    v_clean = v.rstrip("Z")
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            dt = datetime.strptime(v_clean, fmt)
            if is_utc:
                dt = dt + timedelta(hours=tz_offset_hours)
            return dt
        except ValueError:
            continue
    return None


def parse_ics_events(raw_ics: str, tz_offset_hours: int = 3) -> list[dict]:
    lines = _unfold_ics(raw_ics)
    events = []
    cur = None
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            cur = {"attendees": []}
            continue
        if ln == "END:VEVENT":
            if cur and cur.get("starts_at"):
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in ln:
            continue
        prop, val = ln.split(":", 1)
        prop_name = prop.split(";", 1)[0].upper()
        val_clean = _unescape_ics(val.strip())
        if prop_name == "UID":
            cur["uid"] = val_clean[:200]
        elif prop_name == "SUMMARY":
            cur["summary"] = val_clean[:250]
        elif prop_name == "DESCRIPTION":
            cur["description"] = val_clean
        elif prop_name == "LOCATION":
            cur["location"] = val_clean[:300]
        elif prop_name == "STATUS":
            cur["status"] = val_clean.upper()
        elif prop_name == "DTSTART":
            cur["starts_at"] = _parse_ics_dt(val_clean, tz_offset_hours=tz_offset_hours)
        elif prop_name == "DTEND":
            cur["ends_at"] = _parse_ics_dt(val_clean, tz_offset_hours=tz_offset_hours)
        elif prop_name == "ATTENDEE":
            m_mail = re.search(r"mailto:([^\s;]+)", val_clean, re.I)
            m_cn = re.search(r"CN=([^;:]+)", prop, re.I)
            cur["attendees"].append({
                "email": m_mail.group(1).strip().lower() if m_mail else None,
                "name": m_cn.group(1).strip('" ') if m_cn else None,
            })
    return events


def _resolve_assignee_id(cfg: CalendarIntegration) -> int | None:
    if cfg.default_assignee_id:
        u = db.session.get(User, cfg.default_assignee_id)
        if u and u.is_active:
            return u.id
    row = db.session.execute(
        db.select(user_companies.c.user_id).where(
            user_companies.c.company_id == cfg.company_id
        )
    ).first()
    return row[0] if row else None


def _extract_contact_from_event(ev: dict) -> tuple[str, str | None, str]:
    summary = (ev.get("summary") or "حجز موعد جديد").strip()
    desc = ev.get("description") or ""
    email = None
    name = None
    for att in ev.get("attendees") or []:
        if att.get("email") and not att["email"].endswith("calendar.google.com"):
            email = att["email"]
            if att.get("name"):
                name = att["name"]
            break
    if not email:
        m_e = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", desc)
        if m_e:
            email = m_e.group(0).lower()
    phone = ""
    m_p = re.search(r"(\+?\d[\d\s\-()]{7,18}\d)", desc)
    if m_p:
        phone = re.sub(r"[^\d+]", "", m_p.group(1))[:25]
    if not phone:
        phone = "غير محدد"
    if not name:
        # Google Bookings titles look like "Consultation (Ahmed Ali)" or "Ahmed Ali - Booking"
        m_paren = re.search(r"\(([^()]{2,80})\)", summary)
        if m_paren:
            name = m_paren.group(1).strip()
        else:
            name = summary[:120]
    return name[:140], email, phone


def upsert_booking_lead(company_id: int, uid: str, starts_at: datetime,
                        ends_at: datetime | None = None,
                        client_name: str = "", email: str | None = None,
                        phone: str = "غير محدد", summary: str = "",
                        description: str = "", location: str = "",
                        assignee_id: int | None = None,
                        cancelled: bool = False) -> str:
    """Idempotent upsert of a Google Booking into Lead + CalendarEvent.
    Returns 'created', 'updated', or 'skipped'."""
    if not assignee_id:
        return "skipped"
    tag = f"[GBOOK-UID:{uid}]"
    existing_lead = Lead.query.filter(
        Lead.company_id == company_id,
        Lead.deleted_at.is_(None),
        Lead.notes.ilike(f"%{tag}%"),
    ).first()
    existing_ev = CalendarEvent.query.filter(
        CalendarEvent.company_id == company_id,
        CalendarEvent.is_deleted.is_(False),
        CalendarEvent.description.ilike(f"%{tag}%"),
    ).first()

    if cancelled:
        changed = False
        if existing_ev and not existing_ev.is_deleted:
            existing_ev.is_deleted = True
            changed = True
        if existing_lead and existing_lead.next_meeting:
            existing_lead.meeting_notes = (existing_lead.meeting_notes or "") + "\n[تم إلغاء الحجز من Google]"
            existing_lead.next_meeting = None
            changed = True
        return "updated" if changed else "skipped"

    if existing_lead:
        changed = False
        if existing_lead.next_meeting != starts_at:
            existing_lead.next_meeting = starts_at
            changed = True
        if existing_ev and existing_ev.starts_at != starts_at:
            existing_ev.starts_at = starts_at
            existing_ev.ends_at = ends_at
            changed = True
        return "updated" if changed else "skipped"

    lead_num = next_number(company_id, "LEAD")
    clean_name = (client_name or summary or "عميل Google Bookings").strip()[:140]
    service_txt = (summary or "حجز موعد عبر Google Bookings").strip()[:250]
    note_body = f"{tag}\nحجز تلقائي من Google Bookings\n{description}".strip()
    lead = Lead(
        company_id=company_id,
        number=lead_num,
        client_name=clean_name,
        email=(email or "").strip().lower() or None,
        phone=(phone or "غير محدد").strip()[:30],
        service_needed=service_txt,
        lead_type="INBOUND",
        source="Google Bookings",
        assigned_to_id=assignee_id,
        created_by_id=assignee_id,
        status=LeadStatus.MEETING_SCHEDULED,
        next_meeting=starts_at,
        meeting_notes=location or None,
        notes=note_body,
    )
    db.session.add(lead)
    db.session.flush()
    db.session.add(LeadStatusEvent(
        lead_id=lead.id,
        from_status=None,
        to_status=LeadStatus.MEETING_SCHEDULED,
        changed_by_id=assignee_id,
        note="تم الإنشاء تلقائياً من حجز Google Bookings",
    ))

    ev = CalendarEvent(
        company_id=company_id,
        created_by_id=assignee_id,
        title=f"📅 حجز: {clean_name}",
        description=f"{tag}\n{service_txt}\n{description}".strip(),
        starts_at=starts_at,
        ends_at=ends_at,
        location=location[:500] if location else None,
    )
    db.session.add(ev)
    return "created"


def sync_company_calendar(cfg: CalendarIntegration) -> dict:
    if not cfg.google_ical_url:
        return {"created": 0, "updated": 0, "skipped": 0, "reason": "no_url"}
    assignee_id = _resolve_assignee_id(cfg)
    if not assignee_id:
        cfg.last_synced_at = datetime.utcnow()
        cfg.last_sync_status = "error"
        cfg.last_sync_error = "لا يوجد مستخدم نشط في الشركة لتعيين الحجوزات إليه."
        db.session.commit()
        return {"created": 0, "updated": 0, "error": cfg.last_sync_error}

    try:
        req = Request(
            cfg.google_ical_url,
            headers={"User-Agent": "MarsoudCalendarSync/1.0"},
        )
        with urlopen(req, timeout=15) as resp:
            raw_ics = resp.read().decode("utf-8", errors="replace")
        events = parse_ics_events(raw_ics)
        cutoff_past = datetime.now() - timedelta(days=7)
        cutoff_future = datetime.now() + timedelta(days=180)
        created = updated = skipped = 0
        for ev in events:
            st = ev.get("starts_at")
            uid = ev.get("uid")
            if not st or not uid or st < cutoff_past or st > cutoff_future:
                skipped += 1
                continue
            name, email, phone = _extract_contact_from_event(ev)
            res = upsert_booking_lead(
                company_id=cfg.company_id,
                uid=uid,
                starts_at=st,
                ends_at=ev.get("ends_at"),
                client_name=name,
                email=email,
                phone=phone,
                summary=ev.get("summary") or "",
                description=ev.get("description") or "",
                location=ev.get("location") or "",
                assignee_id=assignee_id,
                cancelled=(ev.get("status") == "CANCELLED"),
            )
            if res == "created":
                created += 1
            elif res == "updated":
                updated += 1
            else:
                skipped += 1

        cfg.last_synced_at = datetime.utcnow()
        cfg.last_sync_status = "ok"
        cfg.last_sync_error = None
        cfg.synced_count = (cfg.synced_count or 0) + created
        db.session.commit()
        return {"created": created, "updated": updated, "skipped": skipped}
    except Exception as exc:
        db.session.rollback()
        cfg = db.session.get(CalendarIntegration, cfg.id)
        if cfg:
            cfg.last_synced_at = datetime.utcnow()
            cfg.last_sync_status = "error"
            cfg.last_sync_error = str(exc)[:400]
            db.session.commit()
        return {"created": 0, "updated": 0, "error": str(exc)[:200]}


def sync_all_calendar_integrations() -> dict:
    rows = CalendarIntegration.query.filter(
        CalendarIntegration.is_active.is_(True),
        CalendarIntegration.google_ical_url.isnot(None),
    ).all()
    out = {}
    for cfg in rows:
        out[cfg.company_id] = sync_company_calendar(cfg)
    return out
