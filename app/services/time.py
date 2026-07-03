"""Timezone-aware helpers.

Companies operate in their own timezone (default Asia/Riyadh). The cron tick
runs in server time, but date-sensitive logic (recurring journals, reminders)
should evaluate "today" in the company's timezone so a 9 PM Riyadh tick on the
last day of the month doesn't accidentally roll into the next month.
"""
from datetime import datetime, date
try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Py < 3.9 fallback — shouldn't happen, project pins 3.9+
    ZoneInfo = None


def today_in_company_tz(company):
    """Return today's date in the company's configured timezone.

    Falls back to server-local today() if zoneinfo or the tz string is invalid.
    """
    tz_name = getattr(company, "timezone", None) or "Asia/Riyadh"
    if ZoneInfo is None:
        return date.today()
    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return date.today()


def now_in_company_tz(company):
    """Return current datetime in the company's timezone (naive — strip tzinfo)."""
    tz_name = getattr(company, "timezone", None) or "Asia/Riyadh"
    if ZoneInfo is None:
        return datetime.utcnow()
    try:
        return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow()


def to_company_tz_str(dt_utc, company, fmt="%Y-%m-%d %H:%M:%S"):
    """Format a stored UTC datetime as a string in the company's timezone."""
    if dt_utc is None:
        return None
    tz_name = getattr(company, "timezone", None) or "Asia/Riyadh"
    if ZoneInfo is None:
        return dt_utc.strftime(fmt)
    try:
        local_dt = dt_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz_name))
        return local_dt.strftime(fmt)
    except Exception:
        return dt_utc.strftime(fmt)


def to_utc_from_company(dt_local, company):
    """Inverse of to_company_tz_str: take a naive datetime the user
    typed in the company's local timezone and return a naive UTC
    datetime suitable for storage.

    Bug context (Ibrahim 2026-07-03): datetime-local inputs on the
    lead-activity form were being stored as-is (naive local), then
    `company_dt` filter — which correctly assumes stored values are
    UTC — added the company offset a second time on display. Result:
    an 18:30 Riyadh input rendered as 21:30 Riyadh.

    Callers should route every user-typed datetime through this
    helper before writing to the DB.
    """
    if dt_local is None:
        return None
    tz_name = getattr(company, "timezone", None) or "Asia/Riyadh"
    if ZoneInfo is None:
        return dt_local
    try:
        # Tag the naive input as being in the company's zone, convert
        # to UTC, then strip tzinfo so the shape matches every other
        # datetime column in this codebase (all naive UTC).
        return (
            dt_local.replace(tzinfo=ZoneInfo(tz_name))
            .astimezone(ZoneInfo("UTC"))
            .replace(tzinfo=None)
        )
    except Exception:
        return dt_local
