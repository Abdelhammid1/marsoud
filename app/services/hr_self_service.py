"""HR Self-Service — auto-provisioning, activation, history.

Functions here are wired into the existing payroll routes so adding an
employee transparently creates / links a User row, OWNER can activate
PENDING accounts, and EmployeeHistory rows get written automatically.

Conventions:
  - A User can exist independently (e.g. the OWNER). Linking the User
    to an Employee is one-way — we set `User.employee_id`, but we don't
    delete the User when the Employee is removed (FK is SET NULL via
    the migration).
  - On auto-provision the new User gets status=PENDING and a placeholder
    random password — they can't log in until OWNER activates them and
    they set their own password via the email link.
"""
import secrets
from datetime import datetime, timedelta
from flask import current_app, url_for

from app import db
from app.models import (
    User, Employee, EmployeeHistory, EmployeeChangeType,
    Invitation, Role,
)
from app.models.user import user_companies
from app.services.permissions import generate_invite_token


# ─── Auto-provisioning ─────────────────────────────────────────────────
def ensure_user_for_employee(employee, *, actor_id=None, role_code="employee"):
    """Make sure there's a User row linked to this employee.

    - If `employee.email` is empty: raise. Employee form requires email.
    - If a User with that email exists: link them by setting
      `User.employee_id` and return the existing User (no status change).
      We DO NOT overwrite an existing user's role — the caller can change
      it explicitly through the users / settings_roles UI.
    - Otherwise: create a fresh User with status=PENDING and the given
      `role_code` on the company (defaults to "employee"), and a random
      placeholder password.

    Returns (user, was_created: bool).
    """
    email = (employee.email or "").strip().lower()
    if not email:
        raise ValueError("البريد الإلكتروني مطلوب لإنشاء حساب الموظف")

    # Defensive: never let the caller pass owner or client through here.
    if role_code in ("owner", "client") or not role_code:
        role_code = "employee"

    existing = User.query.filter(db.func.lower(User.email) == email).first()
    if existing:
        # Link the User → Employee (overwrite only if it's currently NULL or
        # already pointing at this same employee). Role NOT changed on
        # existing users to avoid downgrading an admin who happens to also
        # be an employee.
        if existing.employee_id in (None, employee.id):
            existing.employee_id = employee.id
        _ensure_membership(existing.id, employee.company_id, role_code=role_code)
        db.session.commit()
        return existing, False

    from app.models import UserStatus
    user = User(
        email=email,
        full_name=employee.name or email.split("@", 1)[0],
        is_active=False,
        status=UserStatus.PENDING.value,
        employee_id=employee.id,
    )
    # Placeholder password the user will never know.
    user.set_password(secrets.token_urlsafe(24))
    db.session.add(user)
    db.session.flush()
    _ensure_membership(user.id, employee.company_id, role_code=role_code)
    db.session.commit()
    return user, True


def _ensure_membership(user_id, company_id, *, role_code):
    """Insert a user_companies row if missing, setting both string and FK."""
    existing = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == user_id) &
            (user_companies.c.company_id == company_id)
        )
    ).first()
    role_row = Role.query.filter_by(
        company_id=company_id, code=role_code
    ).first()
    role_id = role_row.id if role_row else None
    if existing:
        # Only fill in role_id if it's currently NULL — don't overwrite
        # an admin that happens to share an email with an employee.
        if existing.role_id is None:
            db.session.execute(
                user_companies.update().where(
                    (user_companies.c.user_id == user_id) &
                    (user_companies.c.company_id == company_id)
                ).values(role=role_code, role_id=role_id)
            )
        return
    db.session.execute(user_companies.insert().values(
        user_id=user_id, company_id=company_id,
        role=role_code, role_id=role_id,
    ))


# ─── Activation + password-set invitation ───────────────────────────────
def activate_user(user, *, company_id, actor_id=None):
    """Flip a PENDING user to ACTIVE and create a password-set invitation.

    Returns (invitation, accept_url) so the caller can either send the
    email or flash the URL to the OWNER in dev. Idempotent — re-activating
    just rotates the token.
    """
    from app.models import UserStatus
    # Keep status and is_active in sync — both flows check different one.
    user.status = UserStatus.ACTIVE.value
    user.is_active = True

    # Generate a fresh activation token (same shape as invitations).
    payload = {"email": user.email, "company_id": company_id,
               "role": "employee", "kind": "activation",
               "user_id": user.id}
    token = generate_invite_token(payload)
    expires = datetime.utcnow() + timedelta(days=14)
    inv = Invitation(
        company_id=company_id, email=user.email,
        role="employee", token=token,
        invited_by_id=actor_id,
        expires_at=expires,
    )
    db.session.add(inv)
    db.session.commit()

    try:
        accept_url = url_for("invitations.accept", token=token, _external=True)
    except Exception:
        accept_url = f"/invitations/accept/{token}"
    return inv, accept_url


def disable_user(user):
    from app.models import UserStatus
    user.status = UserStatus.DISABLED.value
    user.is_active = False
    db.session.commit()


# ─── EmployeeHistory autolog ────────────────────────────────────────────
def log_history(employee, change_type, old_value, new_value, *,
                changed_by_id=None, notes=None):
    """Append an EmployeeHistory row only if the value actually changed.

    Both sides are stringified to a stable form so the timeline reads
    consistently (e.g. "5000.00" not Decimal('5000')).
    """
    old_s = _norm(old_value)
    new_s = _norm(new_value)
    if old_s == new_s:
        return None
    entry = EmployeeHistory(
        employee_id=employee.id,
        change_type=change_type.value if hasattr(change_type, "value") else change_type,
        old_value=old_s, new_value=new_s,
        changed_by_id=changed_by_id,
        notes=notes,
    )
    db.session.add(entry)
    return entry


def _norm(v):
    if v is None:
        return None
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return f"{float(v):.2f}"
    except Exception:
        pass
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def diff_employee_for_history(employee, form, *, dept_lookup, status_label_lookup):
    """Capture the snapshot of tracked fields BEFORE update_employee
    mutates them. Returns a dict the caller passes to apply_history_log
    AFTER the commit so we have the new values too.

    Tracked: DEPARTMENT, JOB_TITLE, SALARY (basic_salary), STATUS.
    """
    return {
        "old_dept_id": employee.department_id,
        "old_dept_name": dept_lookup(employee.department_id),
        "old_job_title": employee.job_title,
        "old_basic_salary": float(employee.basic_salary or 0),
        "old_status": status_label_lookup(employee.status),
    }


def apply_history_log(employee, before, *, dept_lookup,
                      status_label_lookup, changed_by_id=None):
    """Compare snapshot vs current state and write history rows."""
    log_history(employee, EmployeeChangeType.DEPARTMENT,
                before["old_dept_name"], dept_lookup(employee.department_id),
                changed_by_id=changed_by_id)
    log_history(employee, EmployeeChangeType.JOB_TITLE,
                before["old_job_title"], employee.job_title,
                changed_by_id=changed_by_id)
    log_history(employee, EmployeeChangeType.SALARY,
                before["old_basic_salary"], float(employee.basic_salary or 0),
                changed_by_id=changed_by_id)
    log_history(employee, EmployeeChangeType.STATUS,
                before["old_status"], status_label_lookup(employee.status),
                changed_by_id=changed_by_id)
    db.session.commit()
