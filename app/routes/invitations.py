"""Public invitation accept flow.

GET /invitations/accept/<token> — show the accept page (login or signup).
POST same URL — wire the membership and redirect to dashboard.

No @login_required: the invited user might not have an account yet.
"""
from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_user, current_user
from app import db
from app.models import User, Company, Invitation
from app.models.user import user_companies
from app.services.permissions import parse_invite_token

bp = Blueprint("invitations", __name__)


@bp.route("/accept/<token>", methods=["GET", "POST"])
def accept(token):
    payload = parse_invite_token(token)
    if not payload:
        flash("رابط الدعوة منتهي أو غير صالح", "error")
        return redirect(url_for("auth.login"))

    invitation = Invitation.query.filter_by(token=token).first()
    if not invitation:
        flash("الدعوة غير موجودة", "error")
        return redirect(url_for("auth.login"))
    if invitation.accepted_at:
        flash("هذه الدعوة تم قبولها مسبقاً", "info")
        return redirect(url_for("auth.login"))
    if invitation.revoked_at:
        flash("تم إلغاء هذه الدعوة", "error")
        return redirect(url_for("auth.login"))
    if invitation.expires_at < datetime.utcnow():
        flash("انتهت صلاحية الدعوة", "error")
        return redirect(url_for("auth.login"))

    email = payload["email"]
    company_id = payload["company_id"]
    role = payload["role"]
    kind = payload.get("kind")    # HR-SS — "activation" means set a fresh password

    existing_user = User.query.filter_by(email=email).first()
    company = db.session.get(Company, company_id)

    if request.method == "POST":
        if existing_user and kind == "activation":
            # HR-SS: PENDING employee setting their first password.
            password = request.form.get("password", "")
            if len(password) < 6:
                flash("كلمة المرور (6 أحرف على الأقل) مطلوبة", "error")
                return render_template(
                    "invitations/accept.html",
                    invitation=invitation, company=company,
                    existing_user=existing_user, activation=True,
                )
            existing_user.set_password(password)
            from app.models import UserStatus
            existing_user.status = UserStatus.ACTIVE.value
            existing_user.is_active = True
            user = existing_user
        elif existing_user:
            # Existing user must authenticate
            password = request.form.get("password", "")
            if not existing_user.check_password(password):
                flash("كلمة المرور غير صحيحة", "error")
                return render_template(
                    "invitations/accept.html",
                    invitation=invitation, company=company,
                    existing_user=existing_user,
                )
            user = existing_user
        else:
            # New user — create
            full_name = (request.form.get("full_name") or "").strip()
            password = request.form.get("password", "")
            if not full_name or len(password) < 6:
                flash("الاسم وكلمة المرور (6 أحرف على الأقل) مطلوبة", "error")
                return render_template(
                    "invitations/accept.html",
                    invitation=invitation, company=company,
                    existing_user=None,
                )
            user = User(email=email, full_name=full_name)
            user.set_password(password)
            db.session.add(user)
            db.session.flush()

        # Attach role
        existing_row = db.session.execute(
            user_companies.select().where(
                (user_companies.c.user_id == user.id) &
                (user_companies.c.company_id == company_id)
            )
        ).first()
        if existing_row:
            db.session.execute(
                user_companies.update()
                .where(
                    (user_companies.c.user_id == user.id) &
                    (user_companies.c.company_id == company_id)
                )
                .values(role=role)
            )
        else:
            db.session.execute(user_companies.insert().values(
                user_id=user.id, company_id=company_id, role=role,
            ))

        invitation.accepted_at = datetime.utcnow()
        db.session.commit()

        login_user(user)
        session["active_company_id"] = company_id
        flash(f"مرحباً بك في {company.name}!", "success")
        return redirect(url_for("dashboard.index"))

    return render_template(
        "invitations/accept.html",
        invitation=invitation, company=company,
        existing_user=existing_user,
        activation=(kind == "activation"),
    )
