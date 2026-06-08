"""Manage company members + invitations.

Listing, inviting, role changes, and revocation. All routes require the
`users.manage` permission (owner only) except viewing the member list (admin+).
"""
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, g
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


@bp.route("/")
@login_required
@require_permission("users.view")
def index():
    company_id = g.active_company.id
    # Member list with roles
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == company_id)
    ).fetchall()
    members = []
    for r in rows:
        u = db.session.get(User, r.user_id)
        if u:
            members.append({"user": u, "role": r.role})
    invitations = Invitation.query.filter_by(company_id=company_id).order_by(
        Invitation.created_at.desc()
    ).all()
    return render_template(
        "users/index.html",
        members=members, invitations=invitations,
        roles=INVITABLE_ROLES, role_labels=ROLE_LABELS_AR,
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
    if role not in ALL_ROLES:
        flash("دور غير صالح", "error")
        return redirect(url_for("users.index"))
    if role == "owner":
        flash("لا يمكن إضافة مالك آخر عبر الدعوة — انقل الملكية يدوياً", "error")
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
                .values(role=role)
            )
            db.session.commit()
            flash(f"تم تحديث دور {existing_user.full_name} إلى {ROLE_LABELS_AR.get(role, role)}", "success")
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
    if new_role not in ALL_ROLES:
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
        .values(role=new_role)
    )
    db.session.commit()
    flash("تم تحديث الدور", "success")
    return redirect(url_for("users.index"))


@bp.route("/<int:user_id>/revoke", methods=["POST"])
@login_required
@require_permission("users.manage")
def revoke(user_id):
    if user_id == current_user.id:
        flash("لا يمكنك إزالة نفسك", "error")
        return redirect(url_for("users.index"))
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
