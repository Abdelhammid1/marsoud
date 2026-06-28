"""MARSOUD-ACTLOG-01 — activity-tracking service layer.

Public surface:
  parse_user_agent(ua_string) -> {device_type, device_os, browser}
  start_session(user, request) -> UserSession
  end_session_by_token(token) -> None
  heartbeat(session_id) -> None
  cleanup_idle_sessions(idle_minutes=10, ended_minutes=30) -> {idle, ended}
  log_action(action_type=..., entity_type=..., entity_id=..., ...) -> UserActivityLog
  extract_entity_from_route(route) -> {entity_type, entity_id}
  view_logging_enabled() -> bool
  set_view_logging_enabled(flag) -> None

`log_action` is the single one-line entry point service code calls to
record CREATE / UPDATE / DELETE / EXPORT events. It's wrapped in
try/except so a logging failure NEVER blocks the business action.
"""
import json
import re
import secrets
from datetime import datetime, timedelta

from flask import request, session as flask_session, current_app
from flask_login import current_user

from app import db
from app.models import UserSession, UserActivityLog


SESSION_KEY = "actlog_session_id"
VIEW_TOGGLE_KEY = "actlog_view_enabled"


# ─── User-agent parsing ─────────────────────────────────────────────────
def parse_user_agent(ua):
    """Cheap, dependency-free UA parser. Returns dict with device_type,
    device_os, browser — fields default to None when nothing matches.
    Spec rules from the ticket:
      - Mobile / Android / iPhone   -> MOBILE
      - iPad / Tablet               -> TABLET
      - otherwise                   -> DESKTOP
      - OS: Windows / Mac OS / Android / iPhone|iPad / Linux
      - Browser: Edg / Chrome / Firefox / Safari (in that priority)
    """
    ua = ua or ""
    # Device type — TABLET first (iPad strings also contain "Mobile" on some)
    if re.search(r"iPad|Tablet", ua, re.IGNORECASE):
        device_type = "TABLET"
    elif re.search(r"Mobile|Android|iPhone", ua, re.IGNORECASE):
        device_type = "MOBILE"
    else:
        device_type = "DESKTOP"
    # OS — order matters: iPhone/iPad before generic Mac OS
    if "Windows" in ua:
        device_os = "Windows"
    elif "Android" in ua:
        device_os = "Android"
    elif re.search(r"iPhone|iPad|iOS", ua):
        device_os = "iOS"
    elif "Mac OS" in ua or "Macintosh" in ua:
        device_os = "macOS"
    elif "Linux" in ua:
        device_os = "Linux"
    else:
        device_os = None
    # Browser — Edg (the new Edge) reports both Edg and Chrome; Edg wins.
    if "Edg" in ua:
        browser = "Edge"
    elif "Firefox" in ua:
        browser = "Firefox"
    elif "Chrome" in ua:
        browser = "Chrome"
    elif "Safari" in ua:
        browser = "Safari"
    else:
        browser = "Other"
    return {
        "device_type": device_type,
        "device_os": device_os,
        "browser": browser,
    }


# ─── Session lifecycle ──────────────────────────────────────────────────
def start_session(user, *, company_id=None):
    """Called from auth.login() after Flask-Login marks the user signed in.
    Inserts a UserSession row and stashes its id in the Flask session
    cookie so subsequent requests can link activity to it."""
    ua_raw = (request.headers.get("User-Agent") or "")[:500]
    meta = parse_user_agent(ua_raw)
    sess = UserSession(
        user_id=user.id, company_id=company_id,
        session_token=secrets.token_urlsafe(32),
        login_at=datetime.utcnow(),
        last_seen_at=datetime.utcnow(),
        ip_address=_client_ip(),
        user_agent=ua_raw,
        device_type=meta["device_type"],
        device_os=meta["device_os"],
        browser=meta["browser"],
        status="ACTIVE",
    )
    db.session.add(sess)
    db.session.commit()
    flask_session[SESSION_KEY] = sess.id
    return sess


def end_session_by_token(session_id):
    """Called from auth.logout() — explicit user-initiated end."""
    if not session_id:
        return
    sess = db.session.get(UserSession, session_id)
    if not sess or sess.logout_at is not None:
        return
    sess.logout_at = datetime.utcnow()
    sess.last_seen_at = sess.logout_at
    sess.status = "ENDED"
    db.session.commit()


def heartbeat(session_id):
    """Bump last_seen_at — fired every 5 min by the base-template JS."""
    if not session_id:
        return
    sess = db.session.get(UserSession, session_id)
    if not sess or sess.logout_at is not None:
        return
    sess.last_seen_at = datetime.utcnow()
    if sess.status == "IDLE":
        sess.status = "ACTIVE"
    db.session.commit()


def cleanup_idle_sessions(idle_minutes=10, ended_minutes=30):
    """Cron tick — flip stale sessions to IDLE then ENDED."""
    now = datetime.utcnow()
    idle_cutoff = now - timedelta(minutes=idle_minutes)
    ended_cutoff = now - timedelta(minutes=ended_minutes)
    out = {"idle": 0, "ended": 0}
    # ENDED first so we don't briefly flip them to IDLE then ENDED.
    ended_q = UserSession.query.filter(
        UserSession.status != "ENDED",
        UserSession.last_seen_at < ended_cutoff,
        UserSession.logout_at.is_(None),
    )
    for s in ended_q.all():
        s.status = "ENDED"
        # We deliberately DON'T set logout_at — distinguishes "user
        # actively logged out" from "browser was closed / went away".
        out["ended"] += 1
    idle_q = UserSession.query.filter(
        UserSession.status == "ACTIVE",
        UserSession.last_seen_at < idle_cutoff,
        UserSession.last_seen_at >= ended_cutoff,
    )
    for s in idle_q.all():
        s.status = "IDLE"
        out["idle"] += 1
    if out["idle"] or out["ended"]:
        db.session.commit()
    return out


# ─── Activity logging ───────────────────────────────────────────────────
def log_action(*, action_type, entity_type=None, entity_id=None,
               entity_label=None, extra_data=None, company_id=None,
               user_id=None, route=None, method=None):
    """Generic one-line entry point for service code. Wrapped in
    try/except so a logging hiccup never breaks the business path."""
    try:
        if not view_logging_enabled() and action_type == "VIEW":
            return None
        cid = company_id
        if cid is None:
            try:
                from flask import g
                cid = g.active_company.id if g.get("active_company") else None
            except Exception:
                cid = None
        uid = user_id
        if uid is None and current_user and current_user.is_authenticated:
            uid = current_user.id
        sid = flask_session.get(SESSION_KEY) if flask_session else None
        ip = _client_ip()
        ua = (request.headers.get("User-Agent") or "")[:500] if request else ""
        meta = parse_user_agent(ua)
        row = UserActivityLog(
            company_id=cid, user_id=uid, session_id=sid,
            action_type=action_type,
            entity_type=entity_type, entity_id=entity_id,
            entity_label=entity_label,
            route=route or (request.path if request else None),
            method=method or (request.method if request else None),
            ip_address=ip,
            device_type=meta["device_type"],
            device_os=meta["device_os"],
            browser=meta["browser"],
            extra_data=(json.dumps(extra_data, default=str,
                                    ensure_ascii=False)
                         if extra_data else None),
        )
        db.session.add(row)
        db.session.commit()
        return row
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        current_app.logger.exception("activity log_action failed (silenced)")
        return None


# ─── Route → entity hint ────────────────────────────────────────────────
_ROUTE_ENTITY_MAP = [
    (re.compile(r"^/tasks/(\d+)"),        "task"),
    (re.compile(r"^/invoices/(\d+)"),     "invoice"),
    (re.compile(r"^/vendor-bills/(\d+)"), "vendor_bill"),
    (re.compile(r"^/payroll/run/(\d+)"),  "payroll_run"),
    (re.compile(r"^/payroll/employees/(\d+)"), "employee"),
    (re.compile(r"^/journals/(\d+)"),     "journal"),
    (re.compile(r"^/assets/(\d+)"),       "asset"),
    (re.compile(r"^/leads/(\d+)"),        "lead"),
    (re.compile(r"^/customers/(\d+)"),    "customer"),
    (re.compile(r"^/products/(\d+)"),     "product"),
    (re.compile(r"^/accounts/(\d+)"),     "account"),
    (re.compile(r"^/projects/(\d+)"),     "project"),
]


def extract_entity_from_route(route):
    """Return {entity_type, entity_id} or empty dict if no match."""
    if not route:
        return {}
    for pat, name in _ROUTE_ENTITY_MAP:
        m = pat.match(route)
        if m:
            try:
                return {"entity_type": name, "entity_id": int(m.group(1))}
            except (TypeError, ValueError):
                return {"entity_type": name}
    return {}


# ─── Toggle for VIEW logging (super-admin escape hatch) ────────────────
def view_logging_enabled() -> bool:
    """Honour the platform-settings toggle. Defaults to True if missing."""
    try:
        from app.models import PlatformSetting
        row = PlatformSetting.query.filter_by(key=VIEW_TOGGLE_KEY).first()
        if not row:
            return True
        return (row.value or "").lower() not in ("0", "false", "off", "no")
    except Exception:
        return True


def set_view_logging_enabled(flag: bool):
    from app.models import PlatformSetting
    row = PlatformSetting.query.filter_by(key=VIEW_TOGGLE_KEY).first()
    if not row:
        row = PlatformSetting(key=VIEW_TOGGLE_KEY,
                              value="1" if flag else "0")
        db.session.add(row)
    else:
        row.value = "1" if flag else "0"
    db.session.commit()


# ─── Helpers ────────────────────────────────────────────────────────────
def _client_ip():
    if not request:
        return None
    # Respect X-Forwarded-For when behind a proxy/CDN (use leftmost hop)
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()[:45]
    return (request.remote_addr or "")[:45]
