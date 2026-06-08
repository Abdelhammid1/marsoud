"""HR services — departments, employee profile extensions, contract-expiry cron.

Most HR data lives directly on `Employee` (extended in HR-02) and the
new `Department` model (HR-01). This module bundles the support logic
that doesn't belong in the route layer.
"""
import logging
from datetime import date, timedelta
from app import db
from app.models import Department, Employee, EmployeeStatus, User
from app.models.user import user_companies

logger = logging.getLogger("ledgeros.hr")


# ─── Departments ─────────────────────────────────────────────────────────
class HRError(Exception):
    pass


def create_department(company_id, name, description=None, manager_employee_id=None):
    name = (name or "").strip()
    if not name:
        raise HRError("اسم القسم مطلوب")
    existing = Department.query.filter_by(company_id=company_id, name=name).first()
    if existing:
        raise HRError("يوجد قسم بنفس الاسم")
    d = Department(
        company_id=company_id,
        name=name,
        description=(description or "").strip() or None,
        manager_employee_id=manager_employee_id or None,
    )
    db.session.add(d)
    db.session.commit()
    return d


def update_department(department, *, name=None, description=None, manager_employee_id=None, is_active=None):
    if name is not None:
        name = name.strip()
        if not name:
            raise HRError("اسم القسم مطلوب")
        if name != department.name:
            clash = Department.query.filter_by(
                company_id=department.company_id, name=name
            ).first()
            if clash and clash.id != department.id:
                raise HRError("يوجد قسم بنفس الاسم")
        department.name = name
    if description is not None:
        department.description = description.strip() or None
    if manager_employee_id is not None:
        department.manager_employee_id = manager_employee_id or None
    if is_active is not None:
        department.is_active = bool(is_active)
    db.session.commit()
    return department


def delete_or_archive_department(department):
    """Hard delete if no employees attached, otherwise soft-archive.

    Spec acceptance #1: "with prevent delete when employees attached".
    """
    member_count = Employee.query.filter_by(department_id=department.id).count()
    if member_count > 0:
        department.is_active = False
        db.session.commit()
        return ("archived", member_count)
    db.session.delete(department)
    db.session.commit()
    return ("deleted", 0)


# ─── Contract-expiry alerts (HR-03) ──────────────────────────────────────
def _hr_recipients(company_id):
    """Users who should receive HR notifications for this company.

    Owners, admins, and HR_MANAGERS. Joined via user_companies, filtered by
    is_active so suspended users don't get pinged.
    """
    rows = db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == company_id) &
            (user_companies.c.role.in_(["owner", "admin", "hr_manager"]))
        )
    ).fetchall()
    users = []
    for r in rows:
        u = db.session.get(User, r.user_id)
        if u and (u.is_active is not False) and u.email:
            users.append(u)
    return users


def check_expiring_contracts(today=None):
    """Find every active employee whose contract ends in the next 60 days
    and email the HR recipients of that company. Dedup'd per-day via
    employees.contract_alert_last_sent.

    Returns a summary dict for the cron tick payload.
    """
    today = today or date.today()
    horizon = today + timedelta(days=60)

    employees = Employee.query.filter(
        Employee.status == EmployeeStatus.ACTIVE,
        Employee.contract_end_date.isnot(None),
        Employee.contract_end_date >= today,
        Employee.contract_end_date <= horizon,
    ).all()

    sent = 0
    skipped = 0
    grouped = {}  # company_id -> [(employee, days_left, severity)]
    for emp in employees:
        if emp.contract_alert_last_sent == today:
            skipped += 1
            continue
        days_left = (emp.contract_end_date - today).days
        severity = "red" if days_left <= 30 else "yellow"
        grouped.setdefault(emp.company_id, []).append((emp, days_left, severity))

    # Lazy import to avoid circulars at module load
    from app.services.email import send_email
    from flask import render_template

    for company_id, items in grouped.items():
        recipients = _hr_recipients(company_id)
        if not recipients:
            logger.info("Contract-expiry: no HR recipients for company %s", company_id)
            # Still mark sent so we don't loop tomorrow
            for emp, _days, _sev in items:
                emp.contract_alert_last_sent = today
            db.session.commit()
            continue

        # One email per company summarising all expiring contracts.
        try:
            html = render_template("emails/contract_expiry.html",
                                   company=items[0][0].company,
                                   items=items, today=today)
        except Exception as e:
            logger.warning("Failed to render contract_expiry email: %s", e)
            html = "<p>تنبيه: عقود على وشك الانتهاء.</p>"

        for u in recipients:
            send_email(
                u.email,
                f"تنبيه: عقود على وشك الانتهاء — {items[0][0].company.name}",
                html,
            )
            sent += 1

        # Dedup state — only after we successfully emitted the alerts
        for emp, _days, _sev in items:
            emp.contract_alert_last_sent = today
        db.session.commit()

    return {
        "checked": len(employees),
        "emails_sent": sent,
        "deduped_same_day": skipped,
    }
