# -*- coding: utf-8 -*-
"""Seed one demo employee, with data, for Play Store screenshots.

The mobile app is employee self-service: every screen reads /api/v1/my/*,
so screenshots are only as good as one employee's own records. The
founder account that already exists has no `employees` row at all, which
makes attendance and "my account" return no_employee_record — hence a
purpose-built user rather than reusing what is there.

Writes to the local dev DB only. Every row hangs off this one user, so
`--undo` removes the whole set; the script runs undo first, which makes
re-running it idempotent rather than additive.
"""
import datetime as dt
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

DB = "instance/ledgeros.db"
EMAIL = "sara.demo@marsoud.com"
PASSWORD = "Demo@2026"
CO = 1

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
today = dt.date.today()
now = dt.datetime.now()


def undo():
    row = c.execute("SELECT id FROM users WHERE email=?", (EMAIL,)).fetchone()
    if not row:
        return
    uid = row["id"]
    emp = c.execute(
        "SELECT id FROM employees WHERE user_id=?", (uid,)).fetchone()
    if emp:
        for t in ("attendance_checkins", "employee_daily_reports"):
            c.execute("DELETE FROM %s WHERE employee_id=?" % t, (emp["id"],))
        c.execute("DELETE FROM employees WHERE id=?", (emp["id"],))
    for t in ("tasks", "leads", "task_schedules"):
        c.execute(
            "DELETE FROM %s WHERE assigned_to_id=? AND company_id=?" % t,
            (uid, CO))
    c.execute("DELETE FROM calendar_events WHERE created_by_id=?", (uid,))
    c.execute("DELETE FROM lead_activities WHERE created_by_id=?", (uid,))
    c.execute("DELETE FROM notifications WHERE user_id=?", (uid,))
    c.execute("DELETE FROM user_companies WHERE user_id=?", (uid,))
    c.execute("DELETE FROM users WHERE id=?", (uid,))
    c.commit()


if "--undo" in sys.argv:
    undo()
    print("  removed the demo user and every row that hung off it")
    raise SystemExit

undo()

from werkzeug.security import generate_password_hash  # noqa: E402

c.execute(
    "INSERT INTO users (email, full_name, password_hash, locale, created_at,"
    " is_superadmin, is_active, status, email_verified_at,"
    " failed_login_attempts, terms_accepted_at, terms_version,"
    " requires_approval)"
    " VALUES (?,?,?,?,?,0,1,'active',?,0,?,'1.0',0)",
    (EMAIL, "سارة عبد الرحمن", generate_password_hash(PASSWORD), "ar", now,
     now, now))
uid = c.execute("SELECT id FROM users WHERE email=?", (EMAIL,)).fetchone()["id"]

role = c.execute(
    "SELECT id FROM roles WHERE company_id=? LIMIT 1", (CO,)).fetchone()
c.execute(
    "INSERT INTO user_companies (user_id, company_id, role, role_id)"
    " VALUES (?,?,?,?)", (uid, CO, "employee", role["id"] if role else None))

c.execute(
    "INSERT INTO employees (company_id, employee_number, name, email, phone,"
    " job_title, start_date, contract_type, status, basic_salary, allowances,"
    " deductions, is_active, created_at, user_id)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
    (CO, "EMP-1042", "سارة عبد الرحمن", EMAIL, "01001234567",
     "أخصائي مبيعات أول", today - dt.timedelta(days=420), "FULL_TIME",
     "ACTIVE", 12000, 2500, 0, now, uid))
eid = c.execute(
    "SELECT id FROM employees WHERE user_id=?", (uid,)).fetchone()["id"]

# Checked in today but not out — that is the state that makes the
# attendance screen show its live "انصراف" action rather than a closed day.
c.execute(
    "INSERT INTO attendance_checkins (company_id, employee_id, date,"
    " check_in_time, created_at) VALUES (?,?,?,?,?)",
    (CO, eid, today, now.replace(hour=8, minute=52, second=0, microsecond=0),
     now))
for d in range(1, 15):
    day = today - dt.timedelta(days=d)
    if day.weekday() in (4, 5):      # Fri/Sat weekend
        continue
    c.execute(
        "INSERT INTO attendance_checkins (company_id, employee_id, date,"
        " check_in_time, check_out_time, created_at) VALUES (?,?,?,?,?,?)",
        (CO, eid, day, dt.datetime.combine(day, dt.time(8, 55)),
         dt.datetime.combine(day, dt.time(17, 6)), now))

TASKS = [
    ("متابعة عرض السعر لشركة النيل للتجارة", "HIGH", "IN_PROGRESS", 1),
    ("إعداد تقرير المبيعات الشهري", "MEDIUM", "IN_PROGRESS", 3),
    ("زيارة العميل — مجموعة الفيصل", "HIGH", "TODO", 2),
    ("تحديث بيانات العملاء في النظام", "LOW", "TODO", 6),
    ("مراجعة عقد التوريد السنوي", "MEDIUM", "DONE", -2),
]
for title, pri, status, off in TASKS:
    c.execute(
        "INSERT INTO tasks (company_id, title, assigned_to_id, priority,"
        " status, deadline, created_at, updated_at, created_by_id,"
        " completed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (CO, title, uid, pri, status, today + dt.timedelta(days=off), now, now,
         1, now if status == "DONE" else None))

LEADS = [
    ("مجموعة الفيصل التجارية", "نظام محاسبي متكامل", "معرض",
     "NEGOTIATION", 85000, 2),
    ("شركة النيل للتجارة", "اشتراك سنوي — 25 مستخدم", "إحالة",
     "PROPOSAL_SENT", 120000, 1),
    ("مؤسسة البركة", "ترخيص نقاط بيع", "الموقع", "NEW_LEAD", 35000, 5),
    ("شركة الشرق للمقاولات", "وحدة إدارة المشاريع", "اتصال بارد",
     "CONTACTED", 60000, 4),
]
for i, (name, svc, src, status, val, off) in enumerate(LEADS):
    c.execute(
        "INSERT INTO leads (company_id, client_name, email, phone,"
        " service_needed, source, assigned_to_id, status, next_meeting,"
        " created_at, updated_at, lead_type, expected_value, created_by_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,'company',?,?)",
        (CO, name, "info@example.com", "0100123456%d" % i, svc, src, uid,
         status, dt.datetime.combine(today + dt.timedelta(days=off),
                                     dt.time(11, 0)),
         now - dt.timedelta(days=off * 3), now, val, 1))

for title, rec in [("زيارة العملاء الأسبوعية", "weekly"),
                   ("تقرير المبيعات الشهري", "monthly")]:
    c.execute(
        "INSERT INTO task_schedules (company_id, title, priority,"
        " assigned_to_id, created_by_id, recurrence, start_date, active,"
        " generated_count, created_at, updated_at) VALUES (?,?,?,?,?,?,?,1,0,?,?)",
        (CO, title, "MEDIUM", uid, 1, rec, today - dt.timedelta(days=30),
         now, now))

NOTES = [
    ("task_assigned", "مهمة جديدة",
     'تم إسناد "زيارة العميل — مجموعة الفيصل" إليك', False, 1),
    ("meeting", "تذكير باجتماع",
     "اجتماع مع شركة النيل للتجارة غدًا 11:00 ص", False, 3),
    ("lead", "عميل محتمل جديد", 'تم إسناد "مؤسسة البركة" إليك', True, 20),
]
for kind, title, body, read, hours in NOTES:
    c.execute(
        "INSERT INTO notifications (company_id, user_id, kind, title, body,"
        " read_at, created_at) VALUES (?,?,?,?,?,?,?)",
        (CO, uid, kind, title, body, now if read else None,
         now - dt.timedelta(hours=hours)))

for d, (t, b) in enumerate([
        ("تقرير اليوم", "زيارتان ميدانيتان، وإرسال عرضَي سعر."),
        ("تقرير أمس", "متابعة هاتفية مع 6 عملاء محتملين.")]):
    c.execute(
        "INSERT INTO employee_daily_reports (company_id, employee_id,"
        " report_date, title, body, status, submitted_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (CO, eid, today - dt.timedelta(days=d), t, b, "SUBMITTED", now, now))

# The meetings screen does NOT read leads.next_meeting. It merges two
# sources: company CalendarEvent rows, and LeadActivity rows of type
# MEETING on leads assigned to me. Seeding next_meeting alone left the
# screen empty, so both real sources get rows here.
lead_ids = [r["id"] for r in c.execute(
    "SELECT id FROM leads WHERE assigned_to_id=? ORDER BY id", (uid,))]
for off, hour, subject, lead_i in [
        (1, 11, "عرض تقديمي للنظام المحاسبي", 1),
        (2, 13, "زيارة الموقع ومناقشة المتطلبات", 0),
        (4, 10, "متابعة العرض المرسل", 3)]:
    if lead_i >= len(lead_ids):
        continue
    c.execute(
        "INSERT INTO lead_activities (company_id, lead_id, type, subject,"
        " body, activity_date, created_by_id, created_at)"
        " VALUES (?,?,'MEETING',?,?,?,?,?)",
        (CO, lead_ids[lead_i], subject, "", 
         dt.datetime.combine(today + dt.timedelta(days=off), dt.time(hour, 0)),
         uid, now))

for off, hour, title, loc in [
        (0, 15, "اجتماع الفريق الأسبوعي", "قاعة الاجتماعات"),
        (3, 12, "مراجعة خطة الربع القادم", "المكتب الرئيسي")]:
    start = dt.datetime.combine(today + dt.timedelta(days=off), dt.time(hour, 0))
    c.execute(
        "INSERT INTO calendar_events (company_id, created_by_id, title,"
        " description, starts_at, ends_at, location, reminder_minutes_before,"
        " is_deleted, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,15,0,?,?)",
        (CO, uid, title, "", start, start + dt.timedelta(hours=1), loc,
         now, now))


c.commit()
print("  demo login : %s / %s" % (EMAIL, PASSWORD))
print("  user #%s, employee #%s, company #%s" % (uid, eid, CO))
for table, where in [
        ("tasks", "assigned_to_id=%s" % uid),
        ("leads", "assigned_to_id=%s" % uid),
        ("attendance_checkins", "employee_id=%s" % eid),
        ("notifications", "user_id=%s" % uid),
        ("task_schedules", "assigned_to_id=%s" % uid),
        ("employee_daily_reports", "employee_id=%s" % eid),
        ("lead_activities", "created_by_id=%s" % uid),
        ("calendar_events", "created_by_id=%s" % uid)]:
    n = c.execute("SELECT COUNT(*) FROM %s WHERE %s" % (table, where)).fetchone()[0]
    print("  %-24s %s" % (table, n))
