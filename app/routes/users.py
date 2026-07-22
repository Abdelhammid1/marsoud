"""Manage company members + invitations.

Listing, inviting, role changes, and revocation. All routes require the
`users.manage` permission (owner only) except viewing the member list (admin+).
"""
from datetime import datetime
from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, g,
    jsonify,
)
from flask_login import login_required, current_user
from app import db
from app.models import User, Company, Invitation
from app.models.user import user_companies
from app.services.permissions import (
    require_permission, get_user_role, ALL_ROLES, INVITABLE_ROLES, ROLE_LABELS_AR,
    generate_invite_token,
)
from app.services.email import send_invitation_email

bp = Blueprint("users", __name__)


def _invitable_db_roles(company_id):
    """Per-company list of roles that can be picked in invite/change-role
    dropdowns. Includes every Role except 'owner' (per-company singleton)
    and 'client' (portal-only signup path). Sorted SYSTEM first, then
    CUSTOM (so old default UX is preserved when no custom roles exist)."""
    from app.models import Role
    return Role.query.filter(
        Role.company_id == company_id,
        Role.code != "owner",
        Role.code != "client",
    ).order_by(Role.type.desc(), Role.id).all()


@bp.route("/")
@login_required
@require_permission("users.view")
def index():
    company_id = g.active_company.id
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == company_id)
    ).fetchall()
    members = []
    db_roles = _invitable_db_roles(company_id)
    # Resolve role name_ar for each membership for display
    code_to_label = {r.code: r.name_ar for r in db_roles}
    code_to_label.setdefault("owner", ROLE_LABELS_AR.get("owner", "owner"))
    for r in rows:
        u = db.session.get(User, r.user_id)
        if u:
            members.append({
                "user": u,
                "role": r.role,
                "role_label": code_to_label.get(r.role, ROLE_LABELS_AR.get(r.role, r.role)),
            })
    invitations = Invitation.query.filter_by(company_id=company_id).order_by(
        Invitation.created_at.desc()
    ).all()
    return render_template(
        "users/index.html",
        members=members, invitations=invitations,
        roles=db_roles, role_labels=ROLE_LABELS_AR,
    )


@bp.route("/invite", methods=["POST"])
@login_required
@require_permission("users.manage")
def invite():
    email = (request.form.get("email") or "").strip().lower()
    role = request.form.get("role", "viewer")
    if not email or "@" not in email:
        flash("بريد إلكتروني غير صالح", "error")
        return redirect(url_for("users.index"))
    # MARSOUD-32 — accept any company role (system + custom) except owner/client
    from app.models import Role
    role_row = Role.query.filter_by(
        company_id=g.active_company.id, code=role,
    ).first()
    if not role_row:
        flash("دور غير صالح", "error")
        return redirect(url_for("users.index"))
    if role == "owner":
        flash("لا يمكن إضافة مالك آخر عبر الدعوة — انقل الملكية يدوياً", "error")
        return redirect(url_for("users.index"))

    # MARSOUD-QUOTAS (Abdelhamid 2026-07-22) — refuse when at the users
    # cap under BLOCK mode. Existing users being re-invited to update
    # their role don't count (short-circuits below).
    try:
        from app.services.quotas import check_quota, QuotaBlockedError
        from app.models import QUOTA_USERS
        check_quota(g.active_company, QUOTA_USERS, incoming=1)
    except QuotaBlockedError as e:
        flash(str(e), "error")
        return redirect(url_for("users.index"))

    # If the user already has a role for this company, just update role
    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        current_role = get_user_role(existing_user.id, g.active_company.id)
        if current_role:
            db.session.execute(
                user_companies.update()
                .where(
                    (user_companies.c.user_id == existing_user.id) &
                    (user_companies.c.company_id == g.active_company.id)
                )
                .values(role=role, role_id=role_row.id)
            )
            db.session.commit()
            flash(f"تم تحديث دور {existing_user.full_name} إلى {role_row.name_ar}", "success")
            return redirect(url_for("users.index"))

    # Otherwise create + send an invitation
    token = generate_invite_token({"email": email, "company_id": g.active_company.id, "role": role})
    inv = Invitation(
        company_id=g.active_company.id,
        email=email, role=role,
        token=token,
        invited_by_id=current_user.id,
    )
    db.session.add(inv)
    db.session.commit()

    accept_url = url_for("invitations.accept", token=token, _external=True)
    sent = send_invitation_email(inv, accept_url)
    flash("تم إرسال الدعوة" if sent else "تم إنشاء الدعوة (وضع التطوير: راجع السجلات)", "success")
    return redirect(url_for("users.index"))


@bp.route("/<int:user_id>/role", methods=["POST"])
@login_required
@require_permission("users.manage")
def change_role(user_id):
    new_role = request.form.get("role")
    from app.models import Role
    role_row = Role.query.filter_by(
        company_id=g.active_company.id, code=new_role,
    ).first()
    if not role_row:
        flash("دور غير صالح", "error")
        return redirect(url_for("users.index"))
    if new_role == "owner":
        flash("لا يمكن تعيين مالك آخر عبر هذه الواجهة", "error")
        return redirect(url_for("users.index"))
    # Don't let an owner demote themselves into a non-owner role if they're
    # the only owner (would orphan the company).
    if user_id == current_user.id:
        current_role = get_user_role(current_user.id, g.active_company.id)
        if current_role == "owner" and new_role != "owner":
            other_owners = db.session.execute(
                user_companies.select().where(
                    (user_companies.c.company_id == g.active_company.id) &
                    (user_companies.c.role == "owner") &
                    (user_companies.c.user_id != current_user.id)
                )
            ).first()
            if not other_owners:
                flash("لا يمكنك إنزال دورك — أنت المالك الوحيد", "error")
                return redirect(url_for("users.index"))
    db.session.execute(
        user_companies.update()
        .where(
            (user_companies.c.user_id == user_id) &
            (user_companies.c.company_id == g.active_company.id)
        )
        .values(role=new_role, role_id=role_row.id)
    )
    db.session.commit()
    flash("تم تحديث الدور", "success")
    return redirect(url_for("users.index"))


@bp.route("/<int:user_id>/send-password-link", methods=["POST"])
@login_required
@require_permission("users.manage")
def send_password_link(user_id):
    """MARSOUD-36 follow-up — owner can resend an activation/password-set
    link to ANY member of the company (PENDING or ACTIVE) without going
    through super-admin. Uses the same Invitation+token flow we already
    have for HR-SS PENDING activation."""
    from app.models import User
    from app.services.hr_self_service import activate_user
    target = db.session.get(User, user_id)
    if not target:
        flash("المستخدم غير موجود", "error")
        return redirect(url_for("users.index"))
    # Ensure they're a member of this company.
    row = db.session.execute(
        user_companies.select().where(
            (user_companies.c.user_id == target.id) &
            (user_companies.c.company_id == g.active_company.id)
        )
    ).first()
    if not row:
        flash("المستخدم ليس عضو في هذه الشركة", "error")
        return redirect(url_for("users.index"))
    inv, accept_url = activate_user(
        target, company_id=g.active_company.id, actor_id=current_user.id,
    )
    # Try to send email; fall back to flashing the link.
    sent = False
    try:
        from app.services.email import send_invitation_email
        send_invitation_email(inv, accept_url)
        sent = True
    except Exception:
        from flask import current_app
        current_app.logger.exception("password link email send failed")
    if sent:
        flash(
            f"تم إرسال رابط تعيين كلمة السر لـ {target.full_name}",
            "success",
        )
    else:
        flash(
            f"تم توليد رابط جديد لـ {target.full_name}. "
            f"شاركه يدوياً: {accept_url}",
            "info",
        )
    return redirect(url_for("users.index"))


@bp.route("/<int:user_id>/revoke", methods=["POST"])
@login_required
@require_permission("users.manage")
def revoke(user_id):
    if user_id == current_user.id:
        flash("لا يمكنك إزالة نفسك", "error")
        return redirect(url_for("users.index"))
    # MARSOUD-USER-FILES-CASCADE — wipe the ex-member's uploaded
    # files (DB rows + on-disk bytes) BEFORE severing membership,
    # so we don't leave orphaned files under
    # private_uploads/user_files/<co>/<user_id>/ that no route can
    # reach anymore.
    from app.services.user_files import delete_all_for_user_in_company
    try:
        delete_all_for_user_in_company(
            company_id=g.active_company.id, user_id=user_id,
        )
    except Exception:
        # A cascade failure must not block the revoke itself — the
        # membership removal is the security-critical step. Any left
        # bytes can be swept by the periodic cleanup.
        import logging
        logging.getLogger("marsoud.user_files").exception(
            "cascade sweep failed on revoke user=%s co=%s",
            user_id, g.active_company.id,
        )
    db.session.execute(
        user_companies.delete().where(
            (user_companies.c.user_id == user_id) &
            (user_companies.c.company_id == g.active_company.id)
        )
    )
    db.session.commit()
    flash("تم إزالة العضو من الشركة", "success")
    return redirect(url_for("users.index"))


@bp.route("/invitations/<int:inv_id>/revoke", methods=["POST"])
@login_required
@require_permission("users.manage")
def revoke_invitation(inv_id):
    inv = db.session.get(Invitation, inv_id)
    if not inv or inv.company_id != g.active_company.id:
        flash("غير موجود", "error")
    else:
        inv.revoked_at = datetime.utcnow()
        db.session.commit()
        flash("تم إلغاء الدعوة", "success")
    return redirect(url_for("users.index"))


@bp.route("/api/search")
@login_required
def api_search():
    """MARSOUD-MENTIONS — JSON endpoint the mention dropdown calls
    when the user types `@`. Returns members of the current company
    whose name/email fuzzy-matches the query. Rate is naturally
    limited by the min_query gate + the tiny result cap.

    No permission gate beyond login: any logged-in member can @-mention
    a coworker (mentions are a communication channel, not a data leak
    — the recipient must already be a company member).
    """
    q = (request.args.get("q") or "").strip()
    limit = min(int(request.args.get("limit", 10) or 10), 20)
    if not g.active_company:
        return jsonify([])
    if len(q) < 1:
        return jsonify([])
    # Scope to users who are members of the current company AND active.
    like = f"%{q}%"
    rows = (
        db.session.query(User)
        .join(user_companies, user_companies.c.user_id == User.id)
        .filter(
            user_companies.c.company_id == g.active_company.id,
            User.is_active == True,  # noqa: E712
            db.or_(
                User.full_name.ilike(like),
                User.email.ilike(like),
            ),
        )
        .order_by(User.full_name.asc())
        .limit(limit)
        .all()
    )
    return jsonify([
        {
            "id": u.id,
            "name": u.full_name or u.email,
            "email": u.email,
            # First letter for the avatar circle in the dropdown.
            "initial": (u.full_name or u.email or "?").strip()[:1].upper(),
        }
        for u in rows
    ])
