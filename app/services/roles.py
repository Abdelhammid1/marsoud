"""MARSOUD-32 — role + permission management services."""
from datetime import datetime
from app import db
from app.models import Role, Permission
from app.models.user import user_companies


class RoleError(Exception):
    """Raised for user-facing validation errors in role management."""


# ─── Role CRUD ──────────────────────────────────────────────────────────
def create_custom_role(company_id, name_ar, *, description=None,
                       permission_ids=None, clone_from_role_id=None):
    name_ar = (name_ar or "").strip()
    if not name_ar:
        raise RoleError("اسم الدور مطلوب")
    if Role.query.filter_by(company_id=company_id, name_ar=name_ar).first():
        raise RoleError("يوجد دور بنفس الاسم")

    # Generate a unique code
    base_code = "custom"
    existing_codes = {
        r.code for r in Role.query.filter_by(company_id=company_id).all()
    }
    i = 1
    while f"{base_code}_{i}" in existing_codes:
        i += 1
    code = f"{base_code}_{i}"

    role = Role(
        company_id=company_id, code=code, name_ar=name_ar,
        type="CUSTOM",
        description=(description or "").strip() or None,
    )
    db.session.add(role)
    db.session.flush()

    # Permissions: explicit IDs > clone source > none
    if permission_ids:
        perms = Permission.query.filter(Permission.id.in_(permission_ids)).all()
        role.permissions = perms
    elif clone_from_role_id:
        src = db.session.get(Role, clone_from_role_id)
        if src and src.company_id == company_id:
            role.permissions = list(src.permissions)

    db.session.commit()
    return role


def update_role_name(role, name_ar, description=None):
    if role.is_system:
        raise RoleError("لا يمكن تعديل دور النظام")
    name_ar = (name_ar or "").strip()
    if not name_ar:
        raise RoleError("اسم الدور مطلوب")
    clash = Role.query.filter(
        Role.company_id == role.company_id,
        Role.name_ar == name_ar,
        Role.id != role.id,
    ).first()
    if clash:
        raise RoleError("يوجد دور بنفس الاسم")
    role.name_ar = name_ar
    if description is not None:
        role.description = description.strip() or None
    db.session.commit()
    return role


def delete_role(role):
    if role.is_system:
        raise RoleError("لا يمكن حذف دور النظام")
    # Anyone still assigned to this role? SELECT.rowcount is unreliable
    # across dialects — fetch and count explicitly.
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.role_id == role.id)
    ).fetchall()
    if len(rows) > 0:
        raise RoleError(
            f"الدور مُعيَّن لـ {len(rows)} مستخدم. أعد تعيينهم لدور آخر أولاً."
        )
    db.session.delete(role)
    db.session.commit()


# ─── Permission toggling ────────────────────────────────────────────────
def set_role_permissions(role, permission_ids):
    if role.is_system:
        raise RoleError("لا يمكن تعديل صلاحيات دور النظام — استنسخه بدور مخصص")
    perms = Permission.query.filter(
        Permission.id.in_(list(permission_ids))
    ).all() if permission_ids else []
    role.permissions = perms
    db.session.commit()
    return role


def toggle_role_permission(role, permission_id, *, on):
    if role.is_system:
        raise RoleError("لا يمكن تعديل صلاحيات دور النظام")
    perm = db.session.get(Permission, permission_id)
    if not perm:
        raise RoleError("الصلاحية غير موجودة")
    if on:
        if perm not in role.permissions:
            role.permissions.append(perm)
    else:
        if perm in role.permissions:
            role.permissions.remove(perm)
    db.session.commit()


# ─── Role assignment to users ───────────────────────────────────────────
def assign_user_to_role(user_id, role):
    """Set the user's role on this company to `role`. Inserts the
    user_companies row if missing. Updates BOTH role (string) and role_id
    so the legacy callsites stay in sync.
    """
    existing = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == user_id) &
            (user_companies.c.company_id == role.company_id)
        )
    ).first()
    if existing:
        db.session.execute(
            user_companies.update().where(
                (user_companies.c.user_id == user_id) &
                (user_companies.c.company_id == role.company_id)
            ).values(role=role.code, role_id=role.id)
        )
    else:
        db.session.execute(
            user_companies.insert().values(
                user_id=user_id, company_id=role.company_id,
                role=role.code, role_id=role.id,
            )
        )
    db.session.commit()


def remove_user_from_role(user_id, role):
    """Unlink the user from the role. Doesn't delete the user_companies
    row entirely (the user may still have other access). Just clears
    role_id and the string."""
    db.session.execute(
        user_companies.delete().where(
            (user_companies.c.user_id == user_id) &
            (user_companies.c.company_id == role.company_id)
        )
    )
    db.session.commit()


# ─── UI helpers ─────────────────────────────────────────────────────────
def roles_for_company(company_id, include_client=False):
    q = Role.query.filter_by(company_id=company_id)
    if not include_client:
        q = q.filter(Role.code != "client")
    return q.order_by(
        # SYSTEM first, then CUSTOM, alphabetised by code within each tier
        Role.type.desc(),  # SYSTEM > CUSTOM lexicographically
        Role.id,
    ).all()


def role_member_count(role):
    res = db.session.execute(
        user_companies.select().where(user_companies.c.role_id == role.id)
    ).fetchall()
    return len(res)


def role_members(role):
    """Return User objects currently assigned to this role for the role's
    company."""
    from app.models import User
    rows = db.session.execute(
        user_companies.select().where(
            (user_companies.c.role_id == role.id) &
            (user_companies.c.company_id == role.company_id)
        )
    ).fetchall()
    return [db.session.get(User, r.user_id) for r in rows if r.user_id]


def candidate_users_for_role(role):
    """Users in this company who aren't already in this role."""
    from app.models import User
    rows = db.session.execute(
        user_companies.select().where(
            user_companies.c.company_id == role.company_id
        )
    ).fetchall()
    in_role = {r.user_id for r in rows if r.role_id == role.id}
    return [db.session.get(User, r.user_id) for r in rows if r.user_id not in in_role]


def grouped_permissions():
    """Permissions grouped by group_ar for the UI matrix.

    Returns: [(group_ar, [Permission, ...]), ...] in catalogue order.
    """
    from collections import OrderedDict
    out = OrderedDict()
    for p in Permission.query.order_by(Permission.id).all():
        out.setdefault(p.group_ar, []).append(p)
    return list(out.items())
