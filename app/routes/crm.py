"""MARSOUD-CRM-EXPANSION — modules that live alongside /leads/:

  /crm/campaigns/         — Section 5a (list, add, disable, stats)
  /crm/activities/        — Section 5b (all touchpoints + follow-ups)
  /crm/contacts/          — Section 5c (extra contact persons)
  /crm/analytics/         — Section 5d (dashboards)

Campaign quick-add is exposed as a JSON POST used inline from the
Lead form, so users don't have to leave the page just to add a new
campaign name.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import (
    Blueprint, render_template, request, g, redirect, url_for, flash, jsonify,
)
from flask_login import login_required, current_user
from sqlalchemy import func, or_

from app import db
from app.models import (
    Campaign, Lead, LeadStatus, LeadContact, LeadActivity, LeadActivityType,
    Customer, User,
)
from app.services.permissions import require_permission


bp = Blueprint("crm", __name__)


# ─── Campaigns (§2 + §5a) ───────────────────────────────────────────────
@bp.route("/campaigns/")
@login_required
@require_permission("leads.view")
def campaigns_index():
    cid = g.active_company.id
    campaigns = Campaign.query.filter_by(company_id=cid).order_by(
        Campaign.active.desc(), Campaign.name).all()
    # Per-campaign stats: leads count, WON count, expected value sum
    stats = {}
    for c in campaigns:
        leads = Lead.query.filter_by(
            company_id=cid, campaign_id=c.id
        ).filter(Lead.deleted_at.is_(None)).all()
        stats[c.id] = {
            "leads": len(leads),
            "won": sum(1 for l in leads if l.status == LeadStatus.WON),
            "lost": sum(1 for l in leads if l.status == LeadStatus.LOST),
            "expected": sum(float(l.expected_value or 0) for l in leads),
        }
    return render_template("crm/campaigns.html",
                            campaigns=campaigns, stats=stats)


@bp.route("/campaigns/new", methods=["POST"])
@login_required
@require_permission("leads.manage")
def campaigns_create():
    """Standard HTML form path — used by the Campaigns page."""
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("اسم الحملة مطلوب", "error")
        return redirect(url_for("crm.campaigns_index"))
    existing = Campaign.query.filter_by(
        company_id=g.active_company.id, name=name,
    ).first()
    if existing:
        flash(f"حملة بنفس الاسم موجودة بالفعل", "warning")
        return redirect(url_for("crm.campaigns_index"))
    c = Campaign(
        company_id=g.active_company.id,
        name=name,
        notes=(request.form.get("notes") or "").strip() or None,
        active=True,
        created_by_id=current_user.id,
    )
    db.session.add(c)
    db.session.commit()
    flash(f"تم إنشاء الحملة: {name}", "success")
    return redirect(url_for("crm.campaigns_index"))


@bp.route("/campaigns/quick-add", methods=["POST"])
@login_required
@require_permission("leads.manage")
def campaigns_quick_add():
    """JSON endpoint used by the Lead form's inline "+ حملة جديدة" button.
    Returns the new campaign's id/name so the dropdown can select it
    without a full page reload."""
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "اسم مطلوب"}), 400
    existing = Campaign.query.filter_by(
        company_id=g.active_company.id, name=name,
    ).first()
    if existing:
        return jsonify({"id": existing.id, "name": existing.name,
                          "reused": True})
    c = Campaign(
        company_id=g.active_company.id, name=name,
        active=True, created_by_id=current_user.id,
    )
    db.session.add(c)
    db.session.commit()
    return jsonify({"id": c.id, "name": c.name, "reused": False})


@bp.route("/campaigns/<int:cid>/toggle", methods=["POST"])
@login_required
@require_permission("leads.manage")
def campaigns_toggle(cid):
    c = Campaign.query.filter_by(
        id=cid, company_id=g.active_company.id
    ).first_or_404()
    c.active = not c.active
    db.session.commit()
    flash(f"الحملة {'مُفعّلة' if c.active else 'معطّلة'}", "success")
    return redirect(url_for("crm.campaigns_index"))


# ─── Activities (§5b) ───────────────────────────────────────────────────
@bp.route("/activities/")
@login_required
@require_permission("leads.view")
def activities_index():
    cid = g.active_company.id
    q = LeadActivity.query.filter_by(company_id=cid)
    type_f = (request.args.get("type") or "").upper()
    lead_id_f = request.args.get("lead_id", type=int)
    if type_f:
        try:
            q = q.filter(LeadActivity.type == LeadActivityType[type_f])
        except KeyError:
            pass
    if lead_id_f:
        q = q.filter(LeadActivity.lead_id == lead_id_f)
    activities = q.order_by(LeadActivity.activity_date.desc()).limit(200).all()

    # Due follow-ups (today + past)
    today = date.today()
    due = LeadActivity.query.filter(
        LeadActivity.company_id == cid,
        LeadActivity.follow_up_date.isnot(None),
        LeadActivity.follow_up_date <= today,
    ).order_by(LeadActivity.follow_up_date).all()

    # Upcoming (next 7 days)
    upcoming = LeadActivity.query.filter(
        LeadActivity.company_id == cid,
        LeadActivity.follow_up_date > today,
        LeadActivity.follow_up_date <= today + timedelta(days=7),
    ).order_by(LeadActivity.follow_up_date).all()

    return render_template(
        "crm/activities.html",
        activities=activities, activity_types=LeadActivityType,
        due=due, upcoming=upcoming,
        type_filter=type_f, lead_id_filter=lead_id_f,
    )


@bp.route("/leads/<int:lead_id>/activities/new", methods=["POST"])
@login_required
@require_permission("leads.manage")
def activity_create(lead_id):
    lead = Lead.query.filter_by(
        id=lead_id, company_id=g.active_company.id
    ).first_or_404()
    try:
        atype = LeadActivityType[request.form.get("type", "NOTE").upper()]
    except KeyError:
        atype = LeadActivityType.NOTE
    a = LeadActivity(
        company_id=g.active_company.id,
        lead_id=lead.id,
        type=atype,
        subject=(request.form.get("subject") or "").strip() or None,
        body=(request.form.get("body") or "").strip() or None,
        activity_date=datetime.utcnow(),
        follow_up_date=_parse_date(request.form.get("follow_up_date")),
        created_by_id=current_user.id,
    )
    db.session.add(a)
    db.session.commit()
    flash(f"تم تسجيل: {atype.label_ar}", "success")
    return redirect(request.referrer or url_for("leads.detail", lead_id=lead.id))


# ─── Contacts (§5c) ─────────────────────────────────────────────────────
@bp.route("/contacts/")
@login_required
@require_permission("leads.view")
def contacts_index():
    cid = g.active_company.id
    q = LeadContact.query.filter_by(company_id=cid)
    search = (request.args.get("q") or "").strip()
    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            LeadContact.name.ilike(like),
            LeadContact.email.ilike(like),
            LeadContact.phone.ilike(like),
        ))
    contacts = q.order_by(LeadContact.name).limit(500).all()
    return render_template("crm/contacts.html",
                            contacts=contacts, search=search)


@bp.route("/leads/<int:lead_id>/contacts/new", methods=["POST"])
@login_required
@require_permission("leads.manage")
def contact_create(lead_id):
    lead = Lead.query.filter_by(
        id=lead_id, company_id=g.active_company.id
    ).first_or_404()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("اسم جهة الاتصال مطلوب", "error")
        return redirect(request.referrer or url_for("leads.detail", lead_id=lead.id))
    c = LeadContact(
        company_id=g.active_company.id,
        lead_id=lead.id,
        name=name,
        role=(request.form.get("role") or "").strip() or None,
        email=(request.form.get("email") or "").strip() or None,
        phone=(request.form.get("phone") or "").strip() or None,
        is_primary=bool(request.form.get("is_primary")),
    )
    db.session.add(c)
    db.session.commit()
    flash("تمت إضافة جهة الاتصال", "success")
    return redirect(request.referrer or url_for("leads.detail", lead_id=lead.id))


@bp.route("/contacts/<int:contact_id>/delete", methods=["POST"])
@login_required
@require_permission("leads.manage")
def contact_delete(contact_id):
    c = LeadContact.query.filter_by(
        id=contact_id, company_id=g.active_company.id
    ).first_or_404()
    db.session.delete(c)
    db.session.commit()
    flash("تم حذف جهة الاتصال", "success")
    return redirect(request.referrer or url_for("crm.contacts_index"))


# ─── Analytics (§5d) ────────────────────────────────────────────────────
@bp.route("/analytics/")
@login_required
@require_permission("leads.view")
def analytics():
    cid = g.active_company.id
    base = Lead.query.filter_by(company_id=cid).filter(Lead.deleted_at.is_(None))

    # Stage counts
    stage_counts = {s: base.filter(Lead.status == s).count() for s in LeadStatus}
    total_leads = base.count()
    won_count = stage_counts.get(LeadStatus.WON, 0)
    lost_count = stage_counts.get(LeadStatus.LOST, 0)
    closed = won_count + lost_count
    conversion_rate = (won_count / closed * 100) if closed else 0.0

    # Open leads (not WON/LOST) expected pipeline value
    open_expected = base.filter(
        Lead.status.notin_((LeadStatus.WON, LeadStatus.LOST))
    ).with_entities(func.coalesce(func.sum(Lead.expected_value), 0)).scalar()

    # Best source, best type
    source_stats = dict(base.with_entities(
        Lead.source, func.count(Lead.id)
    ).group_by(Lead.source).all())
    type_stats = dict(base.with_entities(
        Lead.lead_type, func.count(Lead.id)
    ).group_by(Lead.lead_type).all())

    # Campaign performance
    campaign_stats = []
    for c in Campaign.query.filter_by(company_id=cid).order_by(Campaign.name).all():
        cl = base.filter(Lead.campaign_id == c.id).all()
        w = sum(1 for l in cl if l.status == LeadStatus.WON)
        campaign_stats.append({
            "name": c.name,
            "leads": len(cl),
            "won": w,
            "conversion": (w / len(cl) * 100) if cl else 0.0,
            "expected": sum(float(l.expected_value or 0) for l in cl),
        })

    # Average close time (WON leads only)
    won_leads = base.filter(
        Lead.status == LeadStatus.WON,
        Lead.converted_at.isnot(None),
    ).all()
    if won_leads:
        deltas = [(l.converted_at - l.created_at).days for l in won_leads
                    if l.converted_at and l.created_at]
        avg_close_days = sum(deltas) / len(deltas) if deltas else 0
    else:
        avg_close_days = 0

    return render_template(
        "crm/analytics.html",
        stage_counts=stage_counts, total_leads=total_leads,
        won_count=won_count, lost_count=lost_count,
        conversion_rate=conversion_rate,
        open_expected=float(open_expected or 0),
        source_stats=source_stats, type_stats=type_stats,
        campaign_stats=campaign_stats,
        avg_close_days=avg_close_days,
        statuses=LeadStatus,
    )


# ─── Helpers ────────────────────────────────────────────────────────────
def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
