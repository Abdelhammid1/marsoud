"""MARSOUD-32 — /settings/roles admin UI.

Routes for the company-owner's role + permission management page.
Mirrors the lexoffice /admin/settings/roles 2-pane layout: roles list
on the left, role details (admins / permissions tabs) on the right.

All routes are gated by the existing `users.manage` permission so only
owners (and whoever the owner explicitly grants users.manage to) can use
this surface.
"""
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g, abort,
)
from flask_login import login_required, current_user

from app import db
from app.models import Role, Permission, User
from app.services.roles import (
    create_custom_role, update_role_name, delete_role,
    set_role_permissions, assign_user_to_role, remove_user_from_role,
    roles_for_company, role_member_count, role_members,
    candidate_users_for_role, grouped_permissions,
    effective_permission_ids,
    RoleError,
)
from app.services.permissions import require_permission


bp = Blueprint("settings_roles", __name__)


def _role_or_404(role_id):
    r = db.session.get(Role, role_id)
    if not r or r.company_id != g.active_company.id:
        abort(404)
    return r


# ─── Main 2-pane page ───────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("users.manage")
def index():
    cid = g.active_company.id
    all_roles = roles_for_company(cid, include_client=True)

    # Pick which role to focus on the right pane
    selected_id_raw = request.args.get("role_id")
    selected = None
    if selected_id_raw:
        try:
            selected = db.session.get(Role, int(selected_id_raw))
            if selected and selected.company_id != cid:
                selected = None
        except (TypeError, ValueError):
            selected = None
    if selected is None and all_roles:
        # Default to first non-owner role for the focus
        selected = next((r for r in all_roles if r.code != "owner"), all_roles[0])

    tab = request.args.get("tab", "permissions")
    if tab not in ("permissions", "admins"):
        tab = "permissions"

    # Build member-count cache
    counts = {r.id: role_member_count(r) for r in all_roles}

    members = []
    candidates = []
    selected_perm_ids = set()
    # MARSOUD-ROLES-REFLECT-SCOPE (2026-08-09) — grouped_permissions
    # now filters by the company's effective_modules so the owner
    # only sees checkboxes for features their plan actually enables
    # (plus 'settings' + _ALWAYS_READABLE which are always visible).
    groups = grouped_permissions(g.active_company)
    if selected:
        members = role_members(selected)
        candidates = candidate_users_for_role(selected)
        selected_perm_ids = {p.id for p in selected.permissions}

    return render_template(
        "settings/roles.html",
        roles=all_roles, counts=counts,
        selected=selected, tab=tab,
        members=members, candidates=candidates,
        groups=groups, selected_perm_ids=selected_perm_ids,
    )


# ─── Role CRUD ──────────────────────────────────────────────────────────
@bp.route("/new", methods=["POST"])
@login_required
@require_permission("users.manage")
def role_new():
    cid = g.active_company.id
    try:
        clone_raw = request.form.get("clone_from") or None
        clone_id = int(clone_raw) if clone_raw else None
        role = create_custom_role(
            cid,
            request.form.get("name_ar", ""),
            description=request.form.get("description"),
            clone_from_role_id=clone_id,
        )
        flash(f"تم إنشاء دور: {role.name_ar}", "success")
        return redirect(url_for("settings_roles.index", role_id=role.id))
    except RoleError as e:
        flash(str(e), "error")
    return redirect(url_for("settings_roles.index"))


@bp.route("/<int:role_id>/rename", methods=["POST"])
@login_required
@require_permission("users.manage")
def role_rename(role_id):
    role = _role_or_404(role_id)
    try:
        update_role_name(role, request.form.get("name_ar"),
                         request.form.get("description"))
        flash("تم تحديث اسم الدور", "success")
    except RoleError as e:
        flash(str(e), "error")
    return redirect(url_for("settings_roles.index", role_id=role_id))


@bp.route("/<int:role_id>/delete", methods=["POST"])
@login_required
@require_permission("users.manage")
def role_delete(role_id):
    role = _role_or_404(role_id)
    try:
        delete_role(role)
        flash("تم حذف الدور", "success")
        return redirect(url_for("settings_roles.index"))
    except RoleError as e:
        flash(str(e), "error")
        return redirect(url_for("settings_roles.index", role_id=role_id))


@bp.route("/<int:role_id>/clone", methods=["POST"])
@login_required
@require_permission("users.manage")
def role_clone(role_id):
    role = _role_or_404(role_id)
    name = (request.form.get("name_ar")
            or f"نسخة من {role.name_ar}").strip()
    try:
        new_role = create_custom_role(
            g.active_company.id, name,
            description=role.description,
            clone_from_role_id=role.id,
        )
        flash(f"تم استنساخ '{role.name_ar}' إلى '{new_role.name_ar}'", "success")
        return redirect(url_for("settings_roles.index", role_id=new_role.id))
    except RoleError as e:
        flash(str(e), "error")
    return redirect(url_for("settings_roles.index", role_id=role.id))


# ─── Permission toggling ────────────────────────────────────────────────
@bp.route("/<int:role_id>/permissions", methods=["POST"])
@login_required
@require_permission("users.manage")
def role_permissions(role_id):
    role = _role_or_404(role_id)
    raw_ids = request.form.getlist("permission_id")
    try:
        submitted = [int(x) for x in raw_ids if x]
        # MARSOUD-ROLES-REFLECT-SCOPE (2026-08-09) — intersect with
        # the effective set so a crafted POST (or a stale open tab
        # from before a plan downgrade) can't grant a permission
        # the roles-page filter hid. Silent drop: never break the
        # owner mid-save with an error the UI didn't warn about.
        allowed = effective_permission_ids(g.active_company)
        ids = [i for i in submitted if i in allowed]
        set_role_permissions(role, ids)
        flash("تم حفظ الصلاحيات", "success")
    except (RoleError, ValueError) as e:
        flash(str(e), "error")
    return redirect(url_for("settings_roles.index",
                            role_id=role_id, tab="permissions"))


# ─── Member assignment ─────────────────────────────────────────────────
@bp.route("/<int:role_id>/members/add", methods=["POST"])
@login_required
@require_permission("users.manage")
def member_add(role_id):
    role = _role_or_404(role_id)
    try:
        user_id = int(request.form.get("user_id"))
        user = db.session.get(User, user_id)
        if not user:
            raise RoleError("المستخدم غير موجود")
        assign_user_to_role(user_id, role)
        flash(f"تم ربط {user.full_name} بدور {role.name_ar}", "success")
    except (RoleError, ValueError, TypeError) as e:
        flash(str(e), "error")
    return redirect(url_for("settings_roles.index", role_id=role_id, tab="admins"))


@bp.route("/<int:role_id>/members/<int:user_id>/remove", methods=["POST"])
@login_required
@require_permission("users.manage")
def member_remove(role_id, user_id):
    role = _role_or_404(role_id)
    # Safety: don't allow removing the only owner of a company
    if role.code == "owner":
        from app.models.user import user_companies
        rows = db.session.execute(
            user_companies.select().where(
                (user_companies.c.role_id == role.id) &
                (user_companies.c.company_id == role.company_id)
            )
        ).fetchall()
        if len(rows) <= 1:
            flash("لا يمكن إزالة المالك الوحيد للشركة", "error")
            return redirect(url_for("settings_roles.index",
                                    role_id=role_id, tab="admins"))
    remove_user_from_role(user_id, role)
    flash("تم إلغاء الربط", "success")
    return redirect(url_for("settings_roles.index", role_id=role_id, tab="admins"))
