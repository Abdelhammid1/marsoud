"""MARSOUD-P120 — Outbound Google Calendar URLs & RFC-5545 ICS generation."""
from __future__ import annotations
from datetime import datetime, date, timedelta
from urllib.parse import urlencode


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
