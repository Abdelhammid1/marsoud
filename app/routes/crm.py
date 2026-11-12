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
    d_from = _parse_ymd(request.args.get("from"))
    d_to = _parse_ymd(request.args.get("to"))
    campaigns = Campaign.query.filter_by(company_id=cid).order_by(
        Campaign.active.desc(), Campaign.name).all()
    buckets = _campaign_buckets(cid, d_from, d_to)
    stats = {c.id: _finish_bucket(buckets.get(c.id)) for c in campaigns}
    nocamp = buckets.get(None)
    return render_template(
        "crm/campaigns.html",
        campaigns=campaigns, stats=stats,
        nocamp=_finish_bucket(nocamp) if nocamp else None,
        totals=_finish_bucket(_merge_buckets(buckets.values())),
        date_from=d_from.isoformat() if d_from else "",
        date_to=d_to.isoformat() if d_to else "",
    )


def _parse_ymd(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _new_bucket():
    from collections import Counter
    return {"leads": 0, "unqualified": 0, "no_response": 0, "open": 0,
            "won": 0, "lost": 0, "expected": 0.0, "won_value": 0.0,
            "reasons": Counter(), "systems": Counter(),
            "industries": Counter(), "ideas": Counter()}


def _campaign_buckets(cid, d_from, d_to):
    """One query, grouped in Python: {campaign_id or None: bucket}."""
    q = Lead.query.filter_by(company_id=cid).filter(Lead.deleted_at.is_(None))
    if d_from:
        q = q.filter(Lead.created_at >= datetime.combine(d_from, datetime.min.time()))
    if d_to:
        q = q.filter(Lead.created_at < datetime.combine(
            d_to + timedelta(days=1), datetime.min.time()))
    buckets = {}
    for l in q.all():
        b = buckets.setdefault(l.campaign_id, _new_bucket())
        val = float(l.expected_value or 0)
        b["leads"] += 1
        b["expected"] += val
        if l.status == LeadStatus.WON:
            b["won"] += 1
            b["won_value"] += val
        elif l.status == LeadStatus.LOST:
            b["lost"] += 1
        elif l.status == LeadStatus.NO_RESPONSE:
            b["no_response"] += 1
        elif l.status == LeadStatus.UNQUALIFIED:
            b["unqualified"] += 1
            b["reasons"].update(l.unqualified_reasons_list)
        else:
            b["open"] += 1
        b["systems"].update(l.systems_list)
        if l.industry:
            b["industries"][l.industry] += 1
        if l.idea_stage:
            b["ideas"][l.idea_stage] += 1
    return buckets


def _merge_buckets(buckets):
    total = _new_bucket()
    for b in buckets:
        for k in ("leads", "unqualified", "no_response", "open", "won",
                  "lost", "expected", "won_value"):
            total[k] += b[k]
        for k in ("reasons", "systems", "industries", "ideas"):
            total[k].update(b[k])
    return total


def _finish_bucket(b):
    """Add percentages and top-3 lists. Old keys (leads/won/lost/no_response/
    expected) are kept so older templates keep working."""
    b = b or _new_bucket()
    n = b["leads"]
    out = {k: v for k, v in b.items() if k not in ("reasons", "systems", "industries", "ideas")}
    out["unq_pct"] = round(b["unqualified"] * 100.0 / n, 1) if n else 0.0
    out["conv_pct"] = round(b["won"] * 100.0 / n, 1) if n else 0.0
    out["top_reasons"] = b["reasons"].most_common(3)
    out["top_systems"] = b["systems"].most_common(3)
    out["top_industries"] = b["industries"].most_common(3)
    out["top_ideas"] = b["ideas"].most_common(3)
    return out


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

    # Asmaa 2026-07-02 — follow_up_date is DateTime now; compare
    # against end-of-today so a follow-up scheduled for today 3PM
    # still shows up in "due" once it's past.
    today = date.today()
    today_end = datetime.combine(today, datetime.max.time())
    horizon = datetime.combine(today + timedelta(days=7),
                                 datetime.max.time())
    due = LeadActivity.query.filter(
        LeadActivity.company_id == cid,
        LeadActivity.follow_up_date.isnot(None),
        LeadActivity.follow_up_date <= today_end,
    ).order_by(LeadActivity.follow_up_date).all()

    upcoming = LeadActivity.query.filter(
        LeadActivity.company_id == cid,
        LeadActivity.follow_up_date > today_end,
        LeadActivity.follow_up_date <= horizon,
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
    # Asmaa 2026-07-02 — activity_date + follow_up_date are now BOTH
    # datetime-local pickers so the user can backdate what happened and
    # schedule a follow-up at a specific hour ("الاجتماع الساعة 3").
    #
    # Ibrahim 2026-07-03 BUG-FIX — datetime-local inputs are naive
    # LOCAL time (company timezone). We must convert to UTC before
    # storage so the company_dt filter — which correctly assumes UTC
    # for every stored datetime — doesn't add the company offset a
    # second time on display. Without this, 18:30 typed by a Riyadh
    # user rendered as 21:30 (bug reported on lead 81, activity id=9).
    from app.services.time import to_utc_from_company

    def _parse_dt(s):
        if not s:
            return None
        s = s.strip()
        if not s:
            return None
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S",
                     "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                local_dt = datetime.strptime(s, fmt)
            except ValueError:
                continue
            return to_utc_from_company(local_dt, g.active_company)
        return None
    posted_at = _parse_dt(request.form.get("activity_date")) or datetime.utcnow()
    # MARSOUD-CRM-STATUS-ACTIVITY-SPLIT (Abdelhamid 2026-07-15) —
    # accept an outcome + emit a status-change suggestion. The
    # suggestion is stored in the session flash so the lead detail
    # page can render a "هل ترغب في تحديث الحالة؟ [نعم] [لا]"
    # panel on the redirect target. Never auto-changes the status.
    outcome = (request.form.get("outcome") or "").strip() or None
    a = LeadActivity(
        company_id=g.active_company.id,
        lead_id=lead.id,
        type=atype,
        subject=(request.form.get("subject") or "").strip() or None,
        body=(request.form.get("body") or "").strip() or None,
        activity_date=posted_at,
        follow_up_date=_parse_dt(request.form.get("follow_up_date")),
        outcome=outcome,
        created_by_id=current_user.id,
    )
    db.session.add(a)
    db.session.commit()
    flash(f"تم تسجيل: {atype.label_ar}"
          + (f" — {outcome}" if outcome else ""), "success")

    # Status-change suggestion — never auto-applied.
    from app.services.activity_outcomes import suggest_status
    suggested = suggest_status(atype, outcome, lead.status)
    if suggested is not None:
        # Store the suggestion on the flask session so the lead detail
        # can render the confirm prompt. Cleared once the user answers
        # yes/no (see leads.suggest_status_apply / _dismiss).
        from flask import session
        session["status_suggestion"] = {
            "lead_id": lead.id,
            "suggested": suggested.name,
            "suggested_label": suggested.label_ar,
            "activity_id": a.id,
        }
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


@bp.route("/contacts/<int:contact_id>/edit", methods=["POST"])
@login_required
@require_permission("leads.manage")
def contact_edit(contact_id):
    """MARSOUD-LEAD-AUTOCONTACT (Abdelhamid 2026-07-13) — the ticket
    requires that a user can EDIT a contact after creation (not just
    delete + recreate). Inline POST from the lead detail page — a
    dedicated GET form was overkill given the 5-field surface."""
    c = LeadContact.query.filter_by(
        id=contact_id, company_id=g.active_company.id
    ).first_or_404()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("اسم جهة الاتصال مطلوب", "error")
        return redirect(request.referrer or url_for("crm.contacts_index"))
    c.name = name
    c.role = (request.form.get("role") or "").strip() or None
    c.email = (request.form.get("email") or "").strip() or None
    c.phone = (request.form.get("phone") or "").strip() or None
    # is_primary is a checkbox — absence in the form means "off".
    c.is_primary = bool(request.form.get("is_primary"))
    db.session.commit()
    flash("تم تحديث جهة الاتصال", "success")
    return redirect(request.referrer or url_for("crm.contacts_index"))


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
# MARSOUD-CRM-P37 - manager view: per-rep numbers + leads that need attention.
# Reuses the helpers of the leads blueprint (follow-up buckets + lead temperature).
def _p37_team():
    from datetime import datetime as _dt
    from app.models import LeadActivity
    from app.routes import leads as _L
    if not _L._can_view_all_leads():
        return None
    cid = g.active_company.id
    all_leads = (Lead.query.filter_by(company_id=cid)
                 .filter(Lead.deleted_at.is_(None)).all())
    openl = [l for l in all_leads
             if l.status.name not in _L._P22_CLOSED and not l.is_converted]
    acts = {}
    ids = [l.id for l in openl]
    for i in range(0, len(ids), 400):
        rows = (LeadActivity.query
                .filter(LeadActivity.company_id == cid,
                        LeadActivity.lead_id.in_(ids[i:i + 400]))
                .order_by(LeadActivity.activity_date.desc(),
                          LeadActivity.id.desc()).all())
        for a in rows:
            acts.setdefault(a.lead_id, []).append(a)
    off = _L._p22_offset(g.active_company)
    now = _dt.utcnow()
    temps = _L._p24_temps(openl, acts)
    reps = {}

    def _rep(l):
        rid = l.assigned_to_id or 0
        if rid not in reps:
            reps[rid] = {
                "id": rid,
                "name": l.assigned_to.full_name if l.assigned_to else "بدون مسؤول",
                "open": 0, "hot": 0, "overdue": 0, "fresh": 0,
                "won": 0, "lost": 0, "value": 0.0,
            }
        return reps[rid]

    att = {"overdue": [], "hot_idle": [], "fresh": [], "unassigned": []}
    for l in openl:
        r = _rep(l)
        al = acts.get(l.id, [])
        last = al[0].activity_date if al else None
        fu = next((a.follow_up_date for a in al if a.follow_up_date), None)
        b = _L._p22_bucket(l, last, fu, now, off)
        t = temps.get(l.id)
        r["open"] += 1
        r["value"] += float(l.expected_value or 0)
        item = {"lead": l, "rep": r["name"], "days": 0, "reason": "",
                "value": float(l.expected_value or 0)}
        if b and b[0] == "overdue":
            r["overdue"] += 1
            att["overdue"].append(dict(item, reason=b[1], days=b[2]))
        if b and b[0] == "fresh" and b[2] >= 1:
            r["fresh"] += 1
            att["fresh"].append(dict(item, reason="جديد من %d يوم ولم يتم التواصل" % b[2], days=b[2]))
        if t and t["t"] == "hot":
            r["hot"] += 1
            if fu is None:
                att["hot_idle"].append(dict(item, reason="ساخن ومفيش متابعة مجدولة"))
        if not l.assigned_to_id:
            att["unassigned"].append(dict(item, reason="مفيش مسؤول"))
    for l in all_leads:
        if l.status == LeadStatus.WON:
            _rep(l)["won"] += 1
        elif l.status == LeadStatus.LOST:
            _rep(l)["lost"] += 1
    rows = sorted(reps.values(), key=lambda x: (-x["open"], -x["won"], x["name"]))
    for r in rows:
        closed = r["won"] + r["lost"]
        r["conv"] = (r["won"] / closed * 100) if closed else None
    att["overdue"].sort(key=lambda x: -x["days"])
    att["fresh"].sort(key=lambda x: -x["days"])
    att["hot_idle"].sort(key=lambda x: -x["value"])
    att["unassigned"].sort(key=lambda x: -x["value"])
    return {
        "reps": rows,
        "att": {k: {"total": len(v), "items": v[:8]} for k, v in att.items()},
    }


@bp.route("/analytics/")
@login_required
@require_permission("leads.view")
def analytics():
    cid = g.active_company.id
    base = Lead.query.filter_by(company_id=cid).filter(Lead.deleted_at.is_(None))

    # Stage counts — keep NO_RESPONSE as its own bucket. Business
    # rules from the ticket: it must NOT be folded into WON or LOST
    # anywhere, and it must NOT count towards the "open pipeline"
    # expected total (the lead is parked, not being worked).
    stage_counts = {s: base.filter(Lead.status == s).count() for s in LeadStatus}
    total_leads = base.count()
    won_count = stage_counts.get(LeadStatus.WON, 0)
    lost_count = stage_counts.get(LeadStatus.LOST, 0)
    no_response_count = stage_counts.get(LeadStatus.NO_RESPONSE, 0)
    closed = won_count + lost_count
    conversion_rate = (won_count / closed * 100) if closed else 0.0

    # Open leads (not WON, LOST, or NO_RESPONSE) expected pipeline value.
    open_expected = base.filter(
        Lead.status.notin_((
            LeadStatus.WON, LeadStatus.LOST, LeadStatus.NO_RESPONSE, LeadStatus.UNQUALIFIED,
        ))
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
            "no_response": sum(
                1 for l in cl if l.status == LeadStatus.NO_RESPONSE),
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
        no_response_count=no_response_count,
        conversion_rate=conversion_rate,
        open_expected=float(open_expected or 0),
        source_stats=source_stats, type_stats=type_stats,
        campaign_stats=campaign_stats,
        avg_close_days=avg_close_days,
        statuses=LeadStatus,
        p37=_p37_team(),
    )


# ─── Helpers ────────────────────────────────────────────────────────────
def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
