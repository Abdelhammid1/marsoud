"""CRM — Leads blueprint.

Routes live at `/leads/*` as a native Marsoud module (not nested under
`/opsflow/`). All queries are scoped to `g.active_company.id`; sales reps
see only their own leads, sales managers / admins / owners see all.
"""
from datetime import datetime, timedelta
from flask import Blueprint, render_template, redirect, url_for, flash, request, g, abort
from flask_login import login_required, current_user
from sqlalchemy import or_

from app import db
from app.models import (
    Lead, LeadStatus, LeadType, LeadSource, LeadStatusEvent,
    LeadComment, User, Customer, Employee,
    Campaign, LeadContact, LeadActivity, LeadActivityType,
)
from app.models.user import user_companies
from app.services.crm import (
    change_lead_status, convert_lead_to_project, CRMError,
)
from app.services.permissions import (
    require_permission, get_user_role, has_permission,
)

bp = Blueprint("leads", __name__)


# ─── Visibility helpers ──────────────────────────────────────────────────
# Legacy role list kept as a fallback only — the canonical check is the
# leads.view_all permission, which is auto-attached to these roles on boot
# via roles_seed.seed_system_roles_for_company(). The fallback covers the
# narrow window between deploy and first boot of the re-sync.
FULL_VISIBILITY = {"owner", "admin", "sales_manager"}


def _can_view_all_leads():
    """MARSOUD-PERM-FIX-01 — permission-based. Custom roles get full-visibility
    by being granted `leads.view_all`, not by having a hardcoded role name."""
    if has_permission("leads.view_all"):
        return True
    role = get_user_role(current_user.id, g.active_company.id)
    return role in FULL_VISIBILITY


def _company_user_ids():
    """All user ids that belong to the active company — used to scope dropdowns."""
    cid = g.active_company.id
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == cid)
    ).fetchall()
    return {r.user_id for r in rows}


def _sales_reps():
    """All active company members who can own a lead. Excludes client-portal
    accounts (those linked to a Customer). Role-agnostic so custom roles
    (role_id-based) are included too, not just legacy string roles."""
    cid = g.active_company.id
    rows = db.session.execute(
        user_companies.select().where(user_companies.c.company_id == cid)
    ).fetchall()
    seen = set()
    reps = []
    for r in rows:
        if r.user_id in seen:
            continue
        seen.add(r.user_id)
        u = db.session.get(User, r.user_id)
        if u and u.is_active and u.linked_customer_id is None:
            reps.append(u)
    return reps


def _project_managers():
    cid = g.active_company.id
    rows = db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == cid) &
            (user_companies.c.role.in_(
                ["project_manager", "admin", "owner"],
            ))
        )
    ).fetchall()
    return [db.session.get(User, r.user_id) for r in rows]


def _lead_or_403(lead_id):
    lead = db.session.get(Lead, lead_id)
    if not lead or lead.company_id != g.active_company.id:
        abort(404)
    if lead.deleted_at is not None:
        # Soft-deleted leads are 404 to everyone in the company panel.
        # Super-admin can still find them via the admin tools.
        abort(404)
    # MARSOUD-LEAD-CREATOR-VIEW (Abdelhamid 2026-07-11) — the creator
    # of the lead can always open it, even when it was later reassigned
    # away. Same rule as tasks: whoever authored the record keeps
    # read access.
    if (not _can_view_all_leads()
            and lead.assigned_to_id != current_user.id
            and lead.created_by_id != current_user.id):
        abort(403)
    return lead


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _validate_enum_value(raw, enum_cls):
    """Accept either an enum member name (e.g. 'INBOUND') or empty/None.
    Returns the enum's .value string when valid, else None.
    Old free-text values that aren't valid enum keys get cleared to None
    on edit — but the dropdown is the only way to set them now."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return enum_cls[raw].value
    except KeyError:
        return None


def _parse_campaign_id(raw):
    """Empty/None → None; string → int if it maps to a real campaign
    owned by the active company, else None. Safe against garbage input."""
    if not raw:
        return None
    try:
        cid = int(raw)
    except (TypeError, ValueError):
        return None
    c = Campaign.query.filter_by(
        id=cid, company_id=g.active_company.id
    ).first()
    return c.id if c else None


def _active_campaigns():
    """Every active campaign for the active company, ordered by name."""
    return Campaign.query.filter_by(
        company_id=g.active_company.id, active=True,
    ).order_by(Campaign.name).all()


def _parse_expected_value(raw):
    """MARSOUD-DASH-01 — clean numeric input from the lead form. Accepts
    empty/None, returns None; otherwise float (or None on garbage)."""
    if raw is None:
        return None
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


def _parse_datetime_local(s):
    """Parse a <input type="datetime-local"> value and convert to UTC.

    MARSOUD-TZ-BUG (Abdelhamid 2026-07-04) — the browser sends the value
    the user typed, in the user's local timezone (naive). If we store
    that string as-is, `company_dt` (which correctly assumes stored
    values are UTC) will add the company offset a second time on
    display — Abdelhamid saw an 18:44 input come back as 19:10+ on
    the task page (Egypt DST + Riyadh conversion stacking).

    Every user-typed datetime must go through `to_utc_from_company`
    before landing in the DB — same pattern the activity form already
    uses (MARSOUD-TZ-01, commit d012cb4).
    """
    if not s:
        return None
    from app.services.time import to_utc_from_company
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            local = datetime.strptime(s, fmt)
        except (TypeError, ValueError):
            continue
        return to_utc_from_company(local, g.active_company)
    return None


# ─── List + filters ──────────────────────────────────────────────────────
@bp.route("/")
@login_required
@require_permission("leads.view")
def index():
    cid = g.active_company.id
    q = (request.args.get("q") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    rep_filter = (request.args.get("rep") or "").strip()
    # MARSOUD-LEADS-CAMPAIGN-FILTER (Abdelhamid 2026-07-13) — filter
    # the pipeline by campaign name. Same pattern already lives on
    # /leads/no-response; ported here per the ticket.
    campaign_filter = (request.args.get("campaign") or "").strip()
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()

    query = Lead.query.filter_by(company_id=cid).filter(Lead.deleted_at.is_(None))
    if not _can_view_all_leads():
        # MARSOUD-LEAD-CREATOR-VIEW — list includes leads I own OR created.
        query = query.filter(or_(
            Lead.assigned_to_id == current_user.id,
            Lead.created_by_id == current_user.id,
        ))

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Lead.client_name.ilike(like),
            Lead.phone.ilike(like),
            Lead.service_needed.ilike(like),
        ))
    if status_filter:
        try:
            query = query.filter(Lead.status == LeadStatus[status_filter])
        except KeyError:
            pass
    if rep_filter:
        try:
            query = query.filter(Lead.assigned_to_id == int(rep_filter))
        except (TypeError, ValueError):
            pass
    if campaign_filter:
        try:
            query = query.filter(Lead.campaign_id == int(campaign_filter))
        except (TypeError, ValueError):
            pass
    optf = _option_filters()
    query = _apply_option_filters(query, optf)
    df = _parse_date(date_from)
    if df:
        query = query.filter(Lead.created_at >= datetime.combine(df, datetime.min.time()))
    dt = _parse_date(date_to)
    if dt:
        query = query.filter(Lead.created_at < datetime.combine(dt + timedelta(days=1), datetime.min.time()))

    # MARSOUD-CRM-NO-RESPONSE — parked leads live in their own folder
    # page (/leads/no-response). They MUST NOT appear on the main
    # pipeline board or in the pipeline stats strip. Filter them out
    # here rather than at query time so the exact same query still
    # feeds the /no-response route below (deduped code path).
    leads = query.order_by(Lead.created_at.desc()).all()
    _hide = {LeadStatus.NO_RESPONSE}
    _show_unq = status_filter == "UNQUALIFIED" or bool(optf["reason"])
    if not _show_unq:
        _hide.add(LeadStatus.UNQUALIFIED)
    pipeline_leads = [l for l in leads if l.status not in _hide]

    # Status counts within current visibility scope
    base = Lead.query.filter_by(company_id=cid).filter(Lead.deleted_at.is_(None))
    if not _can_view_all_leads():
        base = base.filter(or_(
            Lead.assigned_to_id == current_user.id,
            Lead.created_by_id == current_user.id,
        ))
    status_counts = {s: base.filter(Lead.status == s).count() for s in LeadStatus}
    # Separate metric for the parked count — surfaces the folder in
    # the top strip without dropping it into the Kanban columns.
    no_response_count = status_counts.get(LeadStatus.NO_RESPONSE, 0)

    # MARSOUD-CRM-EXPANSION §1 — Kanban view: group the filtered leads
    # into columns per LeadStatus so the template can render N columns
    # in one loop.
    view = (request.args.get("view") or "board").lower()
    if view not in ("board", "list"):
        view = "board"
    pipeline_statuses = [s for s in LeadStatus if s != LeadStatus.NO_RESPONSE
                         and (s != LeadStatus.UNQUALIFIED
                              or _show_unq)]
    unqualified_count = status_counts.get(LeadStatus.UNQUALIFIED, 0)
    board_statuses = list(LeadStatus)  # board shows every stage, closed ones collapsed
    columns = {s: [] for s in board_statuses}
    for l in leads:
        columns[l.status].append(l)

    return render_template(
        "leads/index.html",
        leads=pipeline_leads,
        statuses=LeadStatus,               # for the filter dropdown
        pipeline_statuses=pipeline_statuses,  # for the Kanban columns
        status_counts=status_counts,
        no_response_count=no_response_count,
        unqualified_count=unqualified_count,
        reps=_sales_reps() if _can_view_all_leads() else [],
        campaigns=_active_campaigns(),
        q=q, status_filter=status_filter, rep_filter=rep_filter,
        campaign_filter=campaign_filter,
        date_from=date_from, date_to=date_to,
        view=view, columns=columns, board_statuses=board_statuses,
    )


# ─── MARSOUD-CRM-NO-RESPONSE (Abdelhamid 2026-07-13) ────────────────────
# Standalone folder page for leads parked at "لا يوجد استجابة". They're
# filtered out of /leads/ entirely so the pipeline board stays clean.
# Restore = post to the existing /leads/<id>/status endpoint with any
# target LeadStatus name; nothing new is needed on the backend for the
# restore action itself.
@bp.route("/no-response")
@login_required
@require_permission("leads.view")
def no_response_index():
    cid = g.active_company.id
    q = (request.args.get("q") or "").strip()
    rep_filter = (request.args.get("rep") or "").strip()
    campaign_filter = (request.args.get("campaign") or "").strip()
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()

    query = (Lead.query.filter_by(company_id=cid)
             .filter(Lead.deleted_at.is_(None))
             .filter(Lead.status == LeadStatus.NO_RESPONSE))
    if not _can_view_all_leads():
        query = query.filter(or_(
            Lead.assigned_to_id == current_user.id,
            Lead.created_by_id == current_user.id,
        ))

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Lead.client_name.ilike(like),
            Lead.phone.ilike(like),
        ))
    if rep_filter:
        try:
            query = query.filter(Lead.assigned_to_id == int(rep_filter))
        except (TypeError, ValueError):
            pass
    if campaign_filter:
        try:
            query = query.filter(Lead.campaign_id == int(campaign_filter))
        except (TypeError, ValueError):
            pass
    df = _parse_date(date_from)
    if df:
        query = query.filter(
            Lead.updated_at >= datetime.combine(df, datetime.min.time()))
    dt = _parse_date(date_to)
    if dt:
        query = query.filter(
            Lead.updated_at < datetime.combine(
                dt + timedelta(days=1), datetime.min.time()))

    leads = query.order_by(Lead.updated_at.desc()).all()

    # `moved_to_no_response_at` per lead: read the latest
    # LeadStatusEvent whose to_status is NO_RESPONSE. Cheap because
    # the parked folder is by definition a small subset.
    move_dates = {}
    for l in leads:
        ev = (LeadStatusEvent.query
              .filter(LeadStatusEvent.lead_id == l.id,
                      LeadStatusEvent.to_status == LeadStatus.NO_RESPONSE)
              .order_by(LeadStatusEvent.created_at.desc())
              .first())
        move_dates[l.id] = ev.created_at if ev else l.updated_at

    return render_template(
        "leads/no_response.html",
        leads=leads,
        statuses=LeadStatus,
        pipeline_statuses=[s for s in LeadStatus
                            if s != LeadStatus.NO_RESPONSE],
        move_dates=move_dates,
        reps=_sales_reps() if _can_view_all_leads() else [],
        campaigns=_active_campaigns(),
        q=q, rep_filter=rep_filter, campaign_filter=campaign_filter,
        date_from=date_from, date_to=date_to,
    )


# ─── MARSOUD: Excel export of leads ──────────────────────────────────────
@bp.route("/export/excel")
@login_required
@require_permission("leads.view")
def export_excel():
    """Download an Excel of leads — honours the same filters as /leads/
    so the file mirrors what's on screen."""
    from flask import send_file
    from app.services.export import export_leads_excel
    cid = g.active_company.id
    q_str = (request.args.get("q") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    rep_filter = (request.args.get("rep") or "").strip()
    # MARSOUD-LEADS-CAMPAIGN-FILTER (2026-07-13) — export honours the
    # same campaign filter as the screen, so the file matches the view.
    campaign_filter = (request.args.get("campaign") or "").strip()
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()

    query = Lead.query.filter_by(company_id=cid).filter(
        Lead.deleted_at.is_(None))
    if not _can_view_all_leads():
        query = query.filter(or_(
            Lead.assigned_to_id == current_user.id,
            Lead.created_by_id == current_user.id,
        ))
    if q_str:
        like = f"%{q_str}%"
        query = query.filter(or_(
            Lead.client_name.ilike(like),
            Lead.phone.ilike(like),
            Lead.service_needed.ilike(like),
        ))
    if status_filter:
        try:
            query = query.filter(Lead.status == LeadStatus[status_filter])
        except KeyError:
            pass
    if campaign_filter:
        try:
            query = query.filter(
                Lead.campaign_id == int(campaign_filter))
        except (TypeError, ValueError):
            pass
    optf = _option_filters()
    query = _apply_option_filters(query, optf)
    if rep_filter:
        try:
            query = query.filter(
                Lead.assigned_to_id == int(rep_filter))
        except (TypeError, ValueError):
            pass
    df = _parse_date(date_from)
    if df:
        query = query.filter(
            Lead.created_at >= datetime.combine(df, datetime.min.time()))
    dt = _parse_date(date_to)
    if dt:
        query = query.filter(
            Lead.created_at < datetime.combine(
                dt + timedelta(days=1), datetime.min.time()))

    leads = query.order_by(Lead.created_at.desc()).all()
    # MARSOUD-LEAD-EXPORT-BY-CAMPAIGN (Abdelhamid 2026-07-15) — when
    # ?group_by=campaign is present, ship the multi-sheet variant
    # (summary + one sheet per campaign). Default stays flat so
    # existing bookmarks / integrations keep working unchanged.
    group_by = (request.args.get("group_by") or "").strip().lower()
    if group_by == "campaign":
        from app.services.export import export_leads_by_campaign_excel
        buf = export_leads_by_campaign_excel(g.active_company, leads)
        fname = f"leads-by-campaign-{g.active_company.id}-{datetime.now():%Y%m%d-%H%M}.xlsx"
    else:
        buf = export_leads_excel(g.active_company, leads)
        fname = f"leads-{g.active_company.id}-{datetime.now():%Y%m%d-%H%M}.xlsx"
    return send_file(
        buf, as_attachment=True, download_name=fname,
        mimetype=("application/vnd.openxmlformats-officedocument."
                  "spreadsheetml.sheet"),
    )


def _option_filters():
    a = request.args
    return {
        "system": [v.strip() for v in a.getlist("system") if v.strip()],
        "industry": (a.get("industry") or "").strip(),
        "idea_stage": (a.get("idea_stage") or "").strip(),
        "reason": [v.strip() for v in a.getlist("reason") if v.strip()],
    }


def _like_json_label(label):
    import json
    needle = json.dumps(label, ensure_ascii=False)
    needle = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + needle + "%"


def _apply_option_filters(query, f):
    if f["system"]:
        query = query.filter(or_(*[
            Lead.systems_needed.like(_like_json_label(s), escape="\\")
            for s in f["system"]]))
    if f["reason"]:
        query = query.filter(or_(*[
            Lead.unqualified_reasons.like(_like_json_label(s), escape="\\")
            for s in f["reason"]]))
    if f["industry"]:
        query = query.filter(Lead.industry == f["industry"])
    if f["idea_stage"]:
        query = query.filter(Lead.idea_stage == f["idea_stage"])
    return query


def _parse_unq_reasons():
    from app.services import lead_options as lo
    return lo.parse_selected(g.active_company.id, "unqualified_reason",
                             request.form.getlist("unqualified_reasons"))


@bp.context_processor
def _lead_option_globals():
    from app.services import lead_options as lo

    def lead_opts(kind):
        try:
            return lo.active_labels(g.active_company.id, kind)
        except Exception:
            db.session.rollback()
            return []
    return {"lead_opts": lead_opts}


def _lead_option_ctx(lead):
    from app.services import lead_options as lo
    cid = g.active_company.id
    return {
        "opt_systems": lo.options_for_form(
            cid, "system", lead.systems_list if lead else []),
        "opt_industries": lo.options_for_form(
            cid, "industry", [lead.industry] if lead and lead.industry else []),
        "opt_idea_stages": lo.options_for_form(
            cid, "idea_stage", [lead.idea_stage] if lead and lead.idea_stage else []),
    }


# ─── CRM option lists admin (phase 4) ─────────────────────────────────────
def _options_redirect(kind=None):
    return redirect(url_for("leads.options") + (f"#k-{kind}" if kind else ""))


@bp.route("/options", methods=["GET"])
@login_required
@require_permission("leads.manage")
def options():
    from app.services import lead_options as lo
    cid = g.active_company.id
    groups = [
        {"kind": k, "title": lo.KIND_LABELS_AR[k],
         "items": lo.get_options(cid, k, include_inactive=True)}
        for k in lo.KINDS
    ]
    return render_template("leads/options.html", groups=groups)


@bp.route("/options/add", methods=["POST"])
@login_required
@require_permission("leads.manage")
def options_add():
    from app.services import lead_options as lo
    from app.models.crm import LeadOption
    cid = g.active_company.id
    kind = (request.form.get("kind") or "").strip()
    label = " ".join((request.form.get("label") or "").split())
    if kind not in lo.KINDS:
        abort(400)
    if not label:
        flash("اكتب الاسم الأول", "error")
        return _options_redirect(kind)
    if len(label) > 120:
        flash("الاسم طويل (الحد الأقصى 120 حرف)", "error")
        return _options_redirect(kind)
    lo.ensure_seeded(cid, kind)
    existing = LeadOption.query.filter_by(
        company_id=cid, kind=kind, label=label).first()
    if existing:
        if existing.is_active:
            flash("الاختيار ده موجود بالفعل", "error")
        else:
            existing.is_active = True
            db.session.commit()
            flash("الاختيار كان مخفي — اتفعّل تاني", "success")
        return _options_redirect(kind)
    last = (db.session.query(db.func.max(LeadOption.sort_order))
            .filter_by(company_id=cid, kind=kind).scalar())
    db.session.add(LeadOption(company_id=cid, kind=kind, label=label,
                              sort_order=(last if last is not None else -1) + 1,
                              is_active=True))
    db.session.commit()
    flash("تمت الإضافة", "success")
    return _options_redirect(kind)


@bp.route("/options/<int:opt_id>/toggle", methods=["POST"])
@login_required
@require_permission("leads.manage")
def options_toggle(opt_id):
    from app.models.crm import LeadOption
    opt = LeadOption.query.filter_by(
        id=opt_id, company_id=g.active_company.id).first_or_404()
    opt.is_active = not opt.is_active
    db.session.commit()
    flash("اتفعّل" if opt.is_active else "اتخفى (العملاء القدام محتفظين بيه)", "success")
    return _options_redirect(opt.kind)


@bp.route("/options/<int:opt_id>/rename", methods=["POST"])
@login_required
@require_permission("leads.manage")
def options_rename(opt_id):
    """Rename an option and carry the new label to existing leads, so filters
    and reports never see two spellings of the same option."""
    import json
    from sqlalchemy.exc import IntegrityError
    from app.services import lead_options as lo
    from app.models.crm import LeadOption
    cid = g.active_company.id
    opt = LeadOption.query.filter_by(id=opt_id, company_id=cid).first_or_404()
    old = opt.label
    new = " ".join((request.form.get("label") or "").split())
    if not new:
        flash("اكتب الاسم الأول", "error")
        return _options_redirect(opt.kind)
    if len(new) > 120:
        flash("الاسم طويل (الحد الأقصى 120 حرف)", "error")
        return _options_redirect(opt.kind)
    if new == old:
        return _options_redirect(opt.kind)
    clash = LeadOption.query.filter(
        LeadOption.company_id == cid, LeadOption.kind == opt.kind,
        LeadOption.label == new, LeadOption.id != opt.id).first()
    if clash:
        flash("فيه اختيار تاني بنفس الاسم ده", "error")
        return _options_redirect(opt.kind)
    updated = 0
    try:
        if opt.kind in ("industry", "idea_stage"):
            updated = (Lead.query
                       .filter(Lead.company_id == cid,
                               getattr(Lead, opt.kind) == old)
                       .update({opt.kind: new}, synchronize_session=False))
        else:
            attr = "systems_needed" if opt.kind == "system" else "unqualified_reasons"
            col = getattr(Lead, attr)
            needle = json.dumps(old, ensure_ascii=False)
            esc = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = (Lead.query
                    .filter(Lead.company_id == cid, col.isnot(None),
                            col.like("%" + esc + "%", escape="\\"))
                    .all())
            for l in rows:
                try:
                    items = json.loads(getattr(l, attr) or "[]")
                except ValueError:
                    continue
                if not isinstance(items, list) or old not in items:
                    continue
                out = []
                for x in items:
                    y = new if x == old else x
                    if y not in out:
                        out.append(y)
                setattr(l, attr, lo.dumps_list(out))
                if opt.kind == "system":
                    l.service_needed = lo.join_labels(out)
                updated += 1
        opt.label = new
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("حصلت مشكلة في الحفظ — جرّب تاني", "error")
        return _options_redirect(opt.kind)
    flash(f"اتعدّل الاسم" + (f" واتحدّث {updated} عميل" if updated else ""), "success")
    return _options_redirect(opt.kind)


@bp.route("/options/<int:opt_id>/move", methods=["POST"])
@login_required
@require_permission("leads.manage")
def options_move(opt_id):
    from app.models.crm import LeadOption
    cid = g.active_company.id
    opt = LeadOption.query.filter_by(id=opt_id, company_id=cid).first_or_404()
    direction = request.form.get("dir")
    if direction not in ("up", "down"):
        abort(400)
    items = (LeadOption.query.filter_by(company_id=cid, kind=opt.kind)
             .order_by(LeadOption.sort_order, LeadOption.id).all())
    i = next(n for n, o in enumerate(items) if o.id == opt.id)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(items):
        items[i], items[j] = items[j], items[i]
    for n, o in enumerate(items):
        o.sort_order = n
    db.session.commit()
    return _options_redirect(opt.kind)


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("leads.manage")
def new():
    if request.method == "POST":
        try:
            assigned_id = int(request.form.get("assigned_to_id"))
            if assigned_id not in _company_user_ids():
                raise CRMError("المسؤول يجب أن يكون من فريق هذه الشركة")
            # PER-CO-NUMBERING (Abdelhamid 2026-07-04) — assign a
            # per-company display number ("L-0001") so a fresh company
            # sees "عميل محتمل #L-0001" not #92 leaked from the global
            # PK. Uses the same next_number() infra as invoices etc.
            from app.services.numbering import next_number
            _lead_number = next_number(g.active_company.id, "LEAD")
            from app.services import lead_options as lo
            _cid = g.active_company.id
            _systems = lo.parse_selected(_cid, "system", request.form.getlist("systems_needed"))
            _service_text = lo.join_labels(_systems)
            lead = Lead(
                company_id=g.active_company.id,
                number=_lead_number,
                client_name=request.form.get("client_name", "").strip(),
                phone=request.form.get("phone", "").strip(),
                email=(request.form.get("email") or "").strip().lower() or None,
                service_needed=_service_text,
                systems_needed=lo.dumps_list(_systems),
                industry=lo.parse_single(_cid, "industry", request.form.get("industry")),
                idea_stage=lo.parse_single(_cid, "idea_stage", request.form.get("idea_stage")),
                lead_type=_validate_enum_value(request.form.get("lead_type"), LeadType),
                source=_validate_enum_value(request.form.get("source"), LeadSource),
                assigned_to_id=assigned_id,
                created_by_id=current_user.id,
                next_meeting=_parse_datetime_local(request.form.get("next_meeting")),
                meeting_notes=(request.form.get("meeting_notes") or "").strip() or None,
                notes=(request.form.get("notes") or "").strip() or None,
                request_description=(request.form.get("request_description") or "").strip() or None,
                sales_action_required=(request.form.get("sales_action_required") or "").strip() or None,
                expected_value=_parse_expected_value(request.form.get("expected_value")),
                campaign_id=_parse_campaign_id(request.form.get("campaign_id")),
                status=LeadStatus.NEW_LEAD,
            )
            if not lead.client_name or not lead.phone or not lead.service_needed:
                raise CRMError("الاسم والهاتف واختيار سيستم واحد على الأقل حقول مطلوبة")
            db.session.add(lead)
            db.session.flush()
            db.session.add(LeadStatusEvent(
                lead_id=lead.id, from_status=None,
                to_status=LeadStatus.NEW_LEAD,
                changed_by_id=current_user.id,
                note="إنشاء العميل المحتمل",
            ))
            # MARSOUD-LEAD-AUTOCONTACT (Abdelhamid 2026-07-13) —
            # every new lead gets a primary Contact automatically,
            # cloning the name + phone from the lead itself. The
            # helper is idempotent so it's also safe to call from
            # the backfill migration + future imports.
            from app.services.crm import ensure_primary_contact
            ensure_primary_contact(lead)
            db.session.commit()
            flash(f"تم إنشاء عميل محتمل: {lead.client_name}", "success")
            return redirect(url_for("leads.detail", lead_id=lead.id))
        except (CRMError, ValueError, TypeError) as e:
            flash(str(e), "error")
    return render_template("leads/form.html", lead=None, **_lead_option_ctx(None),
                           reps=_sales_reps(),
                           lead_types=LeadType, lead_sources=LeadSource,
                           campaigns=_active_campaigns())


@bp.route("/<int:lead_id>")
@login_required
@require_permission("leads.view")
def detail(lead_id):
    lead = _lead_or_403(lead_id)
    can_manage_files = _can_view_all_leads() or lead.assigned_to_id == current_user.id
    # MARSOUD-CRM-EXPANSION §4/5b/5c — surface activities + contacts on
    # the lead detail page so the quick-action buttons work.
    activities = LeadActivity.query.filter_by(
        lead_id=lead.id).order_by(LeadActivity.activity_date.desc()).all()
    contacts = LeadContact.query.filter_by(
        lead_id=lead.id).order_by(LeadContact.is_primary.desc(),
                                    LeadContact.name).all()
    # MARSOUD-CRM-STATUS-ACTIVITY-SPLIT — outcome catalogue for the JS
    # dropdown + any pending status-change suggestion from the last
    # activity save.
    from app.services.activity_outcomes import all_outcomes_json
    from flask import session
    _sugg = session.get("status_suggestion")
    if _sugg and _sugg.get("lead_id") != lead.id:
        _sugg = None
    _p11_next = _p11_next_step(lead, activities)
    _p17_wa = _p17_wa_pack(lead, _p11_next)
    return render_template("leads/detail.html",
                           next_step=_p11_next,
                           wa_text=_p17_wa[1],
                           wa_kind=_p17_wa[0],
                           p19=_p19_files(lead),
                           p20=_p20_invoices(lead),
                           p21=_p21_ctx(lead),
                           p22=_p22_fu(lead),
                           file_kinds=_P19_KINDS,
                           lead=lead, statuses=LeadStatus,
                           lead_types=LeadType, lead_sources=LeadSource,
                           can_manage_files=can_manage_files,
                           activities=activities, contacts=contacts,
                           activity_types=LeadActivityType,
                           activity_outcomes_json=all_outcomes_json(),
                           status_suggestion=_sugg)


# MARSOUD-CRM-P15 — WhatsApp helpers: normalise phone to wa.me digits + first-contact message.
@bp.app_template_filter("wa_digits")
def _p15_wa_digits(raw):
    import re
    s = str(raw or "").strip()
    plus = s.startswith("+")
    d = re.sub(r"\D", "", s)
    if not d:
        return ""
    if plus:
        return d
    if d.startswith("00"):
        return d[2:]
    if d.startswith(("20", "966", "971")) and len(d) in (12, 13):
        return d
    if d.startswith("0") and len(d) == 11:      # Egypt 01x xxxxxxxx
        return "20" + d[1:]
    if d.startswith("05") and len(d) == 10:     # KSA 05x xxxxxxx
        return "966" + d[1:]
    if d.startswith("1") and len(d) == 10:      # Egypt without the leading 0
        return "20" + d
    if d.startswith("5") and len(d) == 9:       # KSA without the leading 0
        return "966" + d
    return d


def _p15_wa_text(lead):
    """Ready-made first-contact WhatsApp text for a lead still in the first stage."""
    if lead.is_converted or lead.status != list(LeadStatus)[0]:
        return None
    parts = (lead.client_name or "").split()
    first = parts[0] if parts else ""
    rep_parts = ((lead.assigned_to.full_name if lead.assigned_to
                  else getattr(current_user, "full_name", "")) or "").split()
    rep = rep_parts[0] if rep_parts else ""
    systems = [x for x in (lead.systems_list or []) if x]
    if not systems and lead.service_needed:
        systems = [lead.service_needed]
    what = ("وصلنا طلبك بخصوص " + " و".join(systems)) if systems else "وصلنا طلبك"
    hello = ("أهلاً %s" % first) if first else "أهلاً بحضرتك"  # MARSOUD-CRM-P16 no emoji (breaks WhatsApp send)
    me = ("معاك %s من منصتي." % rep) if rep else "معاك فريق منصتي."
    return "\n".join([
        hello,
        me,
        what + "، وحابب أتعرف على احتياجك أكتر عشان أرشحلك أنسب حل.",
        "ينفع نتكلم النهارده مكالمة سريعة؟ قولي الوقت المناسب ليك.",
    ])


# MARSOUD-CRM-P17 — pick the WhatsApp message by stage: first contact / after meeting / follow-up.
def _p17_wa_pack(lead, ns):
    """Returns (kind_label, text) or (None, None). ns = _p11_next_step() result (None for closed leads)."""
    from datetime import datetime as _dt
    if ns is None:
        return (None, None)
    first_txt = _p15_wa_text(lead)
    if first_txt:
        return ("أول تواصل", first_txt)
    parts = (lead.client_name or "").split()
    first = parts[0] if parts else ""
    rep_parts = ((lead.assigned_to.full_name if lead.assigned_to
                  else getattr(current_user, "full_name", "")) or "").split()
    rep = rep_parts[0] if rep_parts else ""
    systems = [x for x in (lead.systems_list or []) if x]
    if not systems and lead.service_needed:
        systems = [lead.service_needed]
    what = " و".join(systems) if systems else "طلبك"
    hello = ("أهلاً %s" % first) if first else "أهلاً بحضرتك"
    me = ("معاك %s من منصتي." % rep) if rep else "معاك فريق منصتي."
    mt = lead.next_meeting
    if mt is not None and getattr(mt, "tzinfo", None):
        mt = mt.replace(tzinfo=None)
    if lead.status.name == "MEETING_SCHEDULED" and mt is not None and mt < _dt.utcnow():
        return ("بعد الاجتماع", "\n".join([
            hello,
            me,
            "شكراً لوقتك في الاجتماع، وسعدت بالتعرف على احتياجك في %s." % what,
            "هبعتلك ملخص اللي اتفقنا عليه عشان كل النقاط تبقى واضحة، ولو في أي سؤال أو تعديل أنا معاك.",
        ]))
    if ns.get("last_days") is None or ns["last_days"] >= 3:
        return ("متابعة", "\n".join([
            hello,
            me,
            "حبيت أطمن عليك وأتابع معاك بخصوص %s." % what,
            "لو لسه مهتم قولي الوقت المناسب ليك ونكمل، ولو مش مناسب دلوقتي قولي وأتواصل معاك في وقت تاني.",
        ]))
    return (None, None)


# MARSOUD-CRM-P18 — all quotations uploaded for a lead (history), oldest = عرض 1.
def _p18_quotes(lead):
    from app.models.opsflow_extras import Document
    from app.models.crm import LeadQuotationMeta
    docs = (Document.query
            .filter_by(company_id=lead.company_id, source_type="LEAD", source_id=lead.id)
            .filter(Document.name.like("quotation:%"))
            .order_by(Document.created_at.asc(), Document.id.asc()).all())
    metas = {m.document_id: m for m in
             LeadQuotationMeta.query.filter_by(lead_id=lead.id).all()}
    out = []
    for i, d in enumerate(docs, 1):
        m = metas.get(d.id)
        nm = d.name.split(":", 1)[1].strip() if ":" in d.name else d.name
        out.append({"n": i, "doc": d, "name": nm,
                    "status": (m.status if m else "SENT"),
                    "note": (m.note if m else None)})
    return out


# MARSOUD-CRM-P19 — lead files centre: one Document per upload, prefix of Document.name = kind.
_P19_KINDS = {
    "quotation": {"label": "عروض الأسعار", "icon": "💰", "one": "عرض سعر", "amount": True,
                  "statuses": [("SENT", "تم الإرسال"), ("UNDER_REVIEW", "قيد الدراسة"),
                               ("REJECTED", "مرفوض"), ("ACCEPTED", "مقبول")]},
    "contract": {"label": "العقود", "icon": "📑", "one": "عقد", "amount": True,
                 "statuses": [("DRAFT", "مسودة"), ("SENT_SIGN", "أُرسل للتوقيع"),
                              ("SIGNED", "موقّع"), ("CANCELLED", "ملغي")]},
    "invoice": {"label": "الفواتير", "icon": "🧾", "one": "فاتورة", "amount": True,
                "statuses": [("ISSUED", "صادرة"), ("PART_PAID", "مدفوعة جزئيًا"),
                             ("PAID", "مدفوعة"), ("OVERDUE", "متأخرة")]},
    "study": {"label": "دراسة المشروع", "icon": "📐", "one": "ملف دراسة", "amount": False,
              "statuses": [("DRAFT", "مسودة"), ("REVIEW", "قيد المراجعة"), ("APPROVED", "معتمدة")]},
    "minutes": {"label": "محاضر الاجتماعات", "icon": "📝", "one": "محضر", "amount": False, "statuses": []},
    "client_doc": {"label": "مستندات العميل", "icon": "🪪", "one": "مستند", "amount": False, "statuses": []},
    "payment": {"label": "إيصالات الدفع", "icon": "💳", "one": "إيصال", "amount": True, "statuses": []},
    "other": {"label": "ملفات أخرى", "icon": "📎", "one": "ملف", "amount": False, "statuses": []},
}


def _p19_parse_amount(raw):
    from decimal import Decimal, InvalidOperation
    s = (raw or "").strip().translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٬٫", "0123456789,.")).replace(",", "")
    if not s:
        return None
    try:
        v = Decimal(s)
    except InvalidOperation:
        return None
    return v if 0 <= v < Decimal("1000000000000") else None


def _p19_add_meta(lead, doc, kind):
    from app.models.crm import LeadQuotationMeta
    cfg = _P19_KINDS[kind]
    codes = [c for c, _ in cfg["statuses"]]
    st = (request.form.get("status") or "").strip()
    if st not in codes:
        st = codes[0] if codes else "NA"
    db.session.add(LeadQuotationMeta(
        company_id=lead.company_id, lead_id=lead.id, document_id=doc.id, status=st,
        note=((request.form.get("note") or "").strip()[:500] or None),
        amount=(_p19_parse_amount(request.form.get("amount")) if cfg["amount"] else None)))


def _p19_files(lead):
    """{'by_kind': {kind: [item, ...] oldest first}, 'sums': {kind: {'total','paid'}}}"""
    from decimal import Decimal
    from app.models.opsflow_extras import Document
    from app.models.crm import LeadQuotationMeta
    docs = (Document.query
            .filter_by(company_id=lead.company_id, source_type="LEAD", source_id=lead.id)
            .order_by(Document.created_at.asc(), Document.id.asc()).all())
    metas = {m.document_id: m for m in
             LeadQuotationMeta.query.filter_by(lead_id=lead.id).all()}
    by_kind = {k: [] for k in _P19_KINDS}
    for d in docs:
        pre, sep, rest = (d.name or "").partition(":")
        k = pre.strip() if sep and pre.strip() in _P19_KINDS else "other"
        nm = rest.strip() if sep and pre.strip() in _P19_KINDS else (d.name or "")
        m = metas.get(d.id)
        status = m.status if m else ("SENT" if k == "quotation" else "NA")
        by_kind[k].append({"n": len(by_kind[k]) + 1, "doc": d, "name": nm, "status": status,
                           "note": (m.note if m else None),
                           "amount": (m.amount if m else None)})
    sums = {}
    for k, cfg in _P19_KINDS.items():
        if not cfg["amount"]:
            continue
        tot = Decimal(0)
        paid = Decimal(0)
        for it in by_kind[k]:
            a = it["amount"] or Decimal(0)
            if it["status"] not in ("REJECTED", "CANCELLED"):
                tot += a
            if it["status"] == "PAID":
                paid += a
        sums[k] = {"total": tot, "paid": paid}
    return {"by_kind": by_kind, "sums": sums}


# MARSOUD-CRM-P20 — real Marsoud invoices of the customer this lead was converted to (read-only).
_P20_ST = {
    "DRAFT": "مسودة", "SENT": "مُرسلة", "PARTIALLY_PAID": "مدفوعة جزئيًا", "PAID": "مدفوعة",
    "OVERDUE": "متأخرة", "CANCELLED": "ملغاة", "REFUNDED": "مُستردة",
    "PARTIALLY_REFUNDED": "مُستردة جزئيًا", "VOIDED": "ملغاة (نقطة بيع)", "WRITTEN_OFF": "مشطوبة",
}
_P20_DEAD = ("DRAFT", "CANCELLED", "VOIDED", "REFUNDED", "WRITTEN_OFF")


def _p20_rows(invoices):
    """Pure formatting: list of Invoice-like -> {'rows': [...], 'total','paid','due'} (active invoices only in sums)."""
    from decimal import Decimal
    rows, tot, paid, due = [], Decimal(0), Decimal(0), Decimal(0)
    for inv in invoices:
        st = getattr(inv.status, "value", None) or str(inv.status)
        t = Decimal(inv.total or 0)
        p = Decimal(inv.paid_amount or 0)
        live = st not in _P20_DEAD
        rem = max(t - p, Decimal(0)) if live else Decimal(0)
        if live:
            tot += t
            paid += p
            due += rem
        rows.append({"id": inv.id, "number": inv.number, "status": st,
                     "label": _P20_ST.get(st, st), "currency": inv.currency or "",
                     "total": t, "paid": p, "rem": rem,
                     "date": inv.issue_date.strftime("%Y-%m-%d") if inv.issue_date else ""})
    return {"rows": rows, "total": tot, "paid": paid, "due": due}


def _p20_invoices(lead):
    if not lead.converted_customer_id:
        return None
    from app.models import Invoice
    invs = (Invoice.query
            .filter_by(company_id=lead.company_id, customer_id=lead.converted_customer_id)
            .order_by(Invoice.issue_date.desc(), Invoice.id.desc()).limit(50).all())
    return _p20_rows(invs)


# MARSOUD-CRM-P21 — link a lead to an EXISTING Marsoud customer (same person entered twice).
def _p21_digits(s):
    return "".join(ch for ch in (s or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")) if ch.isdigit())


def _p21_pick(lead, customers):
    """Pure: -> (suggested, others) lists of customers; suggested = same email / phone tail / name."""
    em = (lead.email or "").strip().lower()
    ph = _p21_digits(lead.phone)[-9:]
    nm = (lead.client_name or "").strip().casefold()
    sug, rest = [], []
    for c in customers:
        cph = _p21_digits(c.phone)[-9:]
        cn = (c.name or "").strip().casefold()
        hit = ((em and (c.email or "").strip().lower() == em)
               or (len(ph) >= 7 and cph == ph)
               or (nm and cn and (cn == nm or nm in cn or cn in nm)))
        (sug if hit else rest).append(c)
    return sug, rest


def _p21_ctx(lead):
    if lead.is_converted:
        return None
    if lead.converted_customer_id:
        return {"linked": lead.converted_customer}
    from app.models.partner import Customer
    cs = (Customer.query.filter_by(company_id=lead.company_id, is_active=True)
          .order_by(Customer.name).limit(500).all())
    sug, rest = _p21_pick(lead, cs)
    return {"linked": None, "sug": sug, "rest": rest}


@bp.route("/<int:lead_id>/link-customer", methods=["POST"])
@login_required
@require_permission("leads.manage")
def link_customer(lead_id):
    from app.models.partner import Customer
    lead = _lead_or_403(lead_id)
    back = redirect(url_for("leads.detail", lead_id=lead.id) + "#cust")
    if not (_can_view_all_leads() or lead.assigned_to_id == current_user.id):
        flash("غير مسموح بتعديل هذا العميل المحتمل", "error")
        return back
    if lead.is_converted:
        flash("العميل المحتمل متحوّل لمشروع بالفعل", "error")
        return back
    try:
        cid = int(request.form.get("customer_id") or 0)
    except ValueError:
        cid = 0
    cust = db.session.get(Customer, cid) if cid else None
    if cust is None or cust.company_id != lead.company_id:
        flash("اختاري عميل صحيح من القائمة", "error")
        return back
    lead.converted_customer_id = cust.id
    db.session.commit()
    flash("تم ربط العميل المحتمل بالعميل: " + (cust.name or ""), "success")
    return redirect(url_for("leads.detail", lead_id=lead.id) + "#files-mrinv")


@bp.route("/<int:lead_id>/unlink-customer", methods=["POST"])
@login_required
@require_permission("leads.manage")
def unlink_customer(lead_id):
    lead = _lead_or_403(lead_id)
    back = redirect(url_for("leads.detail", lead_id=lead.id) + "#cust")
    if not (_can_view_all_leads() or lead.assigned_to_id == current_user.id):
        flash("غير مسموح بتعديل هذا العميل المحتمل", "error")
        return back
    if lead.is_converted:
        flash("لا يمكن فك الربط بعد التحويل لمشروع", "error")
        return back
    lead.converted_customer_id = None
    db.session.commit()
    flash("تم فك الربط", "success")
    return back


# MARSOUD-CRM-P22 — "my day" page + quick follow-up buttons.
_P22_CLOSED = ("WON", "LOST", "NO_RESPONSE", "UNQUALIFIED")
_P22_ORDER = ("overdue", "today", "fresh", "stale")
_P22_TITLES = {"overdue": "متابعات متأخرة", "today": "النهارده", "fresh": "عملاء جدد لسه ما اتكلمش معاهم",
               "stale": "راكدين: مفيش تواصل ولا متابعة مجدولة"}


def _p22_offset(company):
    """company-local minus UTC, as a timedelta (falls back to 0)."""
    from datetime import datetime as _d, timedelta as _t
    try:
        from app.services.time import to_utc_from_company
        u = to_utc_from_company(_d(2000, 1, 1, 12), company)
        return _d(2000, 1, 1, 12) - u.replace(tzinfo=None)
    except Exception:
        return _t(0)


def _p22_nv(d):
    return d.replace(tzinfo=None) if (d is not None and getattr(d, "tzinfo", None)) else d


def _p22_bucket(lead, last, fu, now_utc, off, stale_days=7):
    """Pure: -> (bucket, reason, days) or None. last/fu are UTC datetimes or None."""
    if lead.is_converted or lead.status.name in _P22_CLOSED:
        return None
    today = (now_utc + off).date()
    fl = (_p22_nv(fu) + off) if fu is not None else None
    if fl is not None:
        if fl.date() < today:
            d = (today - fl.date()).days
            return ("overdue", "متابعة متأخرة %d يوم" % d, d)
        if fl.date() == today:
            return ("today", "متابعة اليوم الساعة " + fl.strftime("%H:%M"), 0)
    if lead.next_meeting is not None:
        ml = _p22_nv(lead.next_meeting) + off
        if ml.date() == today and ml >= now_utc + off:
            return ("today", "اجتماع اليوم الساعة " + ml.strftime("%H:%M"), 0)
    if last is None:
        d = max(0, (today - (_p22_nv(lead.created_at) + off).date()).days) if lead.created_at else 0
        return ("fresh", "جديد ولم يتم التواصل بعد", d)
    ld = (today - (_p22_nv(last) + off).date()).days
    if fl is None and ld >= stale_days:
        return ("stale", "آخر تواصل من %d يوم ومفيش متابعة مجدولة" % ld, ld)
    return None


def _p22_today(rep_filter=None):
    from datetime import datetime as _dt
    from app.models import LeadActivity
    cid = g.active_company.id
    q = Lead.query.filter_by(company_id=cid).filter(Lead.deleted_at.is_(None))
    all_view = _can_view_all_leads()
    if not all_view:
        q = q.filter(Lead.assigned_to_id == current_user.id)
    elif rep_filter:
        q = q.filter(Lead.assigned_to_id == rep_filter)
    leads = [l for l in q.all() if l.status.name not in _P22_CLOSED and not l.is_converted]
    acts = {}
    ids = [l.id for l in leads]
    for i in range(0, len(ids), 400):
        rows = (LeadActivity.query
                .filter(LeadActivity.company_id == cid, LeadActivity.lead_id.in_(ids[i:i + 400]))
                .order_by(LeadActivity.activity_date.desc(), LeadActivity.id.desc()).all())
        for a in rows:
            acts.setdefault(a.lead_id, []).append(a)
    off = _p22_offset(g.active_company)
    now = _dt.utcnow()
    buckets = {k: [] for k in _P22_ORDER}
    reps = {}
    for l in leads:
        al = acts.get(l.id, [])
        last = al[0].activity_date if al else None
        fu = next((a.follow_up_date for a in al if a.follow_up_date), None)
        r = _p22_bucket(l, last, fu, now, off)
        if r is None:
            continue
        wa_text = None
        try:
            ns = _p11_next_step(l, al)
            wa_text = _p17_wa_pack(l, ns)[1] if ns is not None else None
        except Exception:
            wa_text = None
        buckets[r[0]].append({"lead": l, "reason": r[1], "days": r[2], "can_snooze": bool(al), "wa_text": wa_text})
        if l.assigned_to_id and l.assigned_to is not None:
            reps[l.assigned_to_id] = l.assigned_to.full_name
    for k in _P22_ORDER:
        buckets[k].sort(key=lambda it: -it["days"])
    return {"buckets": buckets, "order": _P22_ORDER, "titles": _P22_TITLES,
            "total": sum(len(v) for v in buckets.values()), "all_view": all_view,
            "reps": sorted(reps.items(), key=lambda kv: kv[1] or ""), "rep_sel": rep_filter}


def _p22_fu(lead):
    """For the lead page: can we schedule (an activity exists, lead is open) + current follow-up text."""
    if lead.is_converted or lead.status.name in _P22_CLOSED:
        return {"can": False, "fu": None}
    from app.models import LeadActivity
    rows = (LeadActivity.query.filter_by(company_id=lead.company_id, lead_id=lead.id)
            .order_by(LeadActivity.activity_date.desc(), LeadActivity.id.desc()).all())
    if not rows:
        return {"can": False, "fu": None}
    fu = next((a.follow_up_date for a in rows if a.follow_up_date), None)
    txt = None
    if fu is not None:
        off = _p22_offset(g.active_company)
        txt = (_p22_nv(fu) + off).strftime("%Y-%m-%d %H:%M")
    return {"can": True, "fu": txt}


@bp.route("/today")
@login_required
@require_permission("leads.view")
def today():
    return render_template("leads/today.html", **_p22_today(request.args.get("rep", type=int)))


@bp.route("/<int:lead_id>/followup", methods=["POST"])
@login_required
@require_permission("leads.manage")
def set_followup(lead_id):
    from datetime import datetime as _dt, timedelta as _td, time as _tm
    from app.models import LeadActivity
    from app.services.time import to_utc_from_company
    lead = _lead_or_403(lead_id)
    nxt = request.form.get("next")

    def back():
        if nxt == "today":
            return redirect(url_for("leads.today"))
        return redirect(url_for("leads.detail", lead_id=lead.id) + "#fu")

    if not (_can_view_all_leads() or lead.assigned_to_id == current_user.id):
        flash("غير مسموح بتعديل هذا العميل المحتمل", "error")
        return back()
    act = (LeadActivity.query.filter_by(company_id=lead.company_id, lead_id=lead.id)
           .order_by(LeadActivity.activity_date.desc(), LeadActivity.id.desc()).first())
    if act is None:
        flash("سجلي أول نشاط للعميل قبل تحديد موعد المتابعة", "error")
        return back()
    today_l = (_dt.utcnow() + _p22_offset(g.active_company)).date()
    try:
        days = int(request.form.get("days") or 0)
    except ValueError:
        days = 0
    d = None
    if days in (1, 3, 7):
        d = today_l + _td(days=days)
    else:
        try:
            d = _dt.strptime((request.form.get("date") or "").strip(), "%Y-%m-%d").date()
        except ValueError:
            d = None
    if d is None or d < today_l or d > today_l + _td(days=365):
        flash("اختاري تاريخ صحيح (من اليوم فصاعدًا)", "error")
        return back()
    act.follow_up_date = to_utc_from_company(_dt.combine(d, _tm(10, 0)), g.active_company)
    db.session.commit()
    flash("تم تحديد المتابعة يوم " + d.strftime("%Y-%m-%d") + " الساعة 10:00", "success")
    return back()


# MARSOUD-CRM-P11 — data for the "next step" bar on the lead page.
def _p11_next_step(lead, activities):
    from datetime import datetime as _dt
    if lead.is_converted or lead.status.name in (
            "WON", "LOST", "NO_RESPONSE", "UNQUALIFIED"):
        return None
    now = _dt.utcnow()

    def nv(d):
        return d.replace(tzinfo=None) if (d is not None and getattr(d, "tzinfo", None)) else d

    last = nv(activities[0].activity_date) if activities else None
    fu = None
    for a in activities:
        if a.follow_up_date:
            fu = nv(a.follow_up_date)
            break
    mt = nv(lead.next_meeting) if lead.next_meeting else None
    if mt is not None and mt < now:
        mt = None
    last_days = max(0, (now.date() - last.date()).days) if last else None
    fu_days = (fu.date() - now.date()).days if fu else None
    fu_overdue = bool(fu and fu < now)
    if fu_overdue:
        level = "red"
    elif last is None:
        level = "amber"
    elif last_days >= 7:
        level = "red"
    elif last_days >= 3:
        level = "amber"
    else:
        level = "green"
    return {"level": level, "last": last, "last_days": last_days,
            "fu": fu, "fu_days": fu_days, "fu_overdue": fu_overdue,
            "meeting": mt}


@bp.route("/<int:lead_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("leads.manage")
def edit(lead_id):
    lead = _lead_or_403(lead_id)
    if request.method == "POST":
        try:
            assigned_id = int(request.form.get("assigned_to_id"))
            if assigned_id not in _company_user_ids():
                raise CRMError("المسؤول يجب أن يكون من فريق هذه الشركة")
            from app.services import lead_options as lo
            _cid = g.active_company.id
            _systems = lo.parse_selected(_cid, "system", request.form.getlist("systems_needed"),
                                         keep=lead.systems_list)
            if not _systems and (lead.systems_list or not lead.service_needed):
                raise CRMError("اختار سيستم واحد على الأقل")
            lead.client_name = request.form.get("client_name", lead.client_name).strip()
            lead.phone = request.form.get("phone", lead.phone).strip()
            lead.email = (request.form.get("email") or "").strip().lower() or None
            if _systems:  # legacy leads with free-text service keep it until a system is picked
                lead.systems_needed = lo.dumps_list(_systems)
                lead.service_needed = lo.join_labels(_systems)
            lead.industry = lo.parse_single(_cid, "industry", request.form.get("industry"),
                                            keep=lead.industry)
            lead.idea_stage = lo.parse_single(_cid, "idea_stage", request.form.get("idea_stage"),
                                              keep=lead.idea_stage)
            lead.lead_type = _validate_enum_value(request.form.get("lead_type"), LeadType)
            lead.source = _validate_enum_value(request.form.get("source"), LeadSource)
            lead.assigned_to_id = assigned_id
            lead.next_meeting = _parse_datetime_local(request.form.get("next_meeting"))
            lead.meeting_notes = (request.form.get("meeting_notes") or "").strip() or None
            lead.notes = (request.form.get("notes") or "").strip() or None
            lead.request_description = (request.form.get("request_description") or "").strip() or None
            lead.sales_action_required = (request.form.get("sales_action_required") or "").strip() or None
            lead.expected_value = _parse_expected_value(request.form.get("expected_value"))
            lead.campaign_id = _parse_campaign_id(request.form.get("campaign_id"))
            db.session.commit()
            flash("تم حفظ التعديلات", "success")
            return redirect(url_for("leads.detail", lead_id=lead.id))
        except (CRMError, ValueError, TypeError) as e:
            flash(str(e), "error")
    return render_template("leads/form.html", lead=lead, **_lead_option_ctx(lead), reps=_sales_reps(),
                           lead_types=LeadType, lead_sources=LeadSource,
                           campaigns=_active_campaigns())


@bp.route("/<int:lead_id>/delete", methods=["POST"])
@login_required
@require_permission("leads.delete")
def delete(lead_id):
    """MARSOUD-47 — soft delete a lead. Only OWNER + admin can do this.
    Converted leads (already turned into a Customer) are refused — the
    Customer/invoices chain stays intact."""
    lead = _lead_or_403(lead_id)
    if lead.is_converted:
        flash(
            "لا يمكن حذف عميل محتمل تم تحويله لمشروع — احذف المشروع/العميل أولاً.",
            "error",
        )
        return redirect(url_for("leads.detail", lead_id=lead.id))
    lead.deleted_at = datetime.utcnow()
    lead.deleted_by_id = current_user.id
    db.session.commit()
    flash(f"تم حذف العميل المحتمل: {lead.client_name}", "success")
    return redirect(url_for("leads.index"))


@bp.route("/<int:lead_id>/status", methods=["POST"])
@login_required
@require_permission("leads.manage")
def status(lead_id):
    lead = _lead_or_403(lead_id)
    try:
        change_lead_status(
            lead, request.form.get("new_status"),
            changed_by_id=current_user.id,
            note=request.form.get("note"),
            lost_reason=request.form.get("lost_reason"),
            unqualified_reasons=_parse_unq_reasons(),
        )
        # MARSOUD-45 — when the new status is "اجتماع مجدول", also persist the
        # date+time the user picked in the conditional inputs.
        if lead.status == LeadStatus.MEETING_SCHEDULED:
            nm = _parse_datetime_local(request.form.get("next_meeting"))
            if nm:
                lead.next_meeting = nm
                db.session.commit()
        flash(f"تم تغيير الحالة إلى: {lead.status.label_ar}", "success")
    except CRMError as e:
        flash(str(e), "error")
    # MARSOUD-CRM-EXPANSION §1 — when the change came from the Kanban
    # board, bounce back to the board instead of the detail page.
    # MARSOUD-CRM-NO-RESPONSE — the parked-folder restore action
    # posts return_to=/leads/no-response so the user lands back on
    # the folder (with the restored lead now missing from it).
    return_to = request.form.get("return_to") or ""
    if return_to == "board":
        return redirect(url_for("leads.index", view="board"))
    if return_to.startswith("/") and not return_to.startswith("//") \
            and "\r" not in return_to and "\n" not in return_to:
        return redirect(return_to)
    return redirect(url_for("leads.detail", lead_id=lead.id))


# ─── Gap-A: lead quotation / contract PDF upload ────────────────────────
@bp.route("/<int:lead_id>/upload/<kind>", methods=["POST"])
@login_required
@require_permission("leads.manage")
def upload(lead_id, kind):
    """FR-04 — upload quotation or contract PDF directly on a lead."""
    from app.services.opsflow_extras import save_document, DocumentError
    from app.models import DocumentSourceType, DocumentVisibility
    if kind not in _P19_KINDS:
        flash("نوع الملف غير معروف", "error")
        return redirect(url_for("leads.detail", lead_id=lead_id))
    lead = _lead_or_403(lead_id)
    file_storage = request.files.get("file")
    try:
        doc = save_document(
            company_id=lead.company_id,
            source_type=DocumentSourceType.LEAD,
            source_id=lead.id,
            file_storage=file_storage,
            visibility=DocumentVisibility.INTERNAL,
            uploaded_by_id=current_user.id,
            name=f"{kind}: {file_storage.filename}" if file_storage else None,
        )
        if kind == "quotation":
            lead.quotation_path = doc.file_path
        elif kind == "contract":
            lead.contract_path = doc.file_path
        _p19_add_meta(lead, doc, kind)  # MARSOUD-CRM-P19
        db.session.commit()
        flash("تم رفع الملف في: " + _P19_KINDS[kind]["label"], "success")
    except DocumentError as e:
        flash(str(e), "error")
    return redirect(url_for("leads.detail", lead_id=lead.id) + "#files-" + kind)


# MARSOUD-CRM-P19 — edit one lead file: status / amount / note (any kind; endpoint name kept)
@bp.route("/<int:lead_id>/quotations/<int:doc_id>/status", methods=["POST"])
@login_required
@require_permission("leads.manage")
def quotation_status(lead_id, doc_id):
    from app.models.opsflow_extras import Document
    from app.models.crm import LeadQuotationMeta
    from datetime import datetime as _dt
    lead = _lead_or_403(lead_id)
    back = redirect(url_for("leads.detail", lead_id=lead.id) + "#files")
    if not (_can_view_all_leads() or lead.assigned_to_id == current_user.id):
        flash("غير مسموح بتعديل ملفات هذا العميل", "error")
        return back
    doc = Document.query.filter_by(id=doc_id, company_id=lead.company_id,
                                   source_type="LEAD", source_id=lead.id).first()
    if doc is None:
        flash("الملف غير موجود", "error")
        return back
    pre, sep, _rest = (doc.name or "").partition(":")
    kind = pre.strip() if sep and pre.strip() in _P19_KINDS else "other"
    back = redirect(url_for("leads.detail", lead_id=lead.id) + "#files-" + kind)
    cfg = _P19_KINDS[kind]
    meta = LeadQuotationMeta.query.filter_by(document_id=doc.id).first()
    if meta is None:
        meta = LeadQuotationMeta(company_id=lead.company_id, lead_id=lead.id,
                                 document_id=doc.id,
                                 status=("SENT" if kind == "quotation" else "NA"))
        db.session.add(meta)
    if "status" in request.form:
        st = (request.form.get("status") or "").strip()
        if st not in [c for c, _ in cfg["statuses"]]:
            db.session.rollback()
            flash("حالة غير معروفة", "error")
            return back
        meta.status = st
    if "amount" in request.form and cfg["amount"]:
        meta.amount = _p19_parse_amount(request.form.get("amount"))
    if "note" in request.form:
        meta.note = (request.form.get("note") or "").strip()[:500] or None
    meta.updated_at = _dt.utcnow()
    db.session.commit()
    flash("تم الحفظ", "success")
    return back


# ─── MARSOUD: comments on leads (ticket C / image #47) ───────────────
@bp.route("/<int:lead_id>/comments", methods=["POST"])
@login_required
@require_permission("leads.view")
def comment_add(lead_id):
    lead = _lead_or_403(lead_id)
    content = (request.form.get("content") or "").strip()
    if not content:
        flash("اكتب نص التعليق قبل الإرسال", "error")
        return redirect(url_for("leads.detail", lead_id=lead.id) + "#comments")
    c = LeadComment(
        lead_id=lead.id,
        user_id=current_user.id,
        company_id=lead.company_id,
        content=content,
    )
    db.session.add(c)
    db.session.commit()
    # MARSOUD-MENTIONS — same fan-out pattern as task comments.
    try:
        from app.services.mentions import parse_mention_ids, notify_mentions
        ids = parse_mention_ids(content)
        if ids:
            notify_mentions(
                actor_user_id=current_user.id,
                mentioned_user_ids=ids,
                company_id=lead.company_id,
                entity_kind="lead",
                entity_label=f"عميل محتمل: {lead.client_name}",
                # MARSOUD-MENTION-EMAIL-FIX (2026-08-13) —
                # _external=True so Gmail/Outlook render the
                # email button as a clickable link.
                link_url=(
                    url_for("leads.detail", lead_id=lead.id,
                             _external=True)
                    + "#comments"
                ),
                snippet=content,
            )
    except Exception:
        import logging
        logging.getLogger("marsoud.mentions").exception(
            "mention fan-out failed on lead %s", lead.id,
        )
    flash("تم إضافة التعليق", "success")
    return redirect(url_for("leads.detail", lead_id=lead.id) + "#comments")


@bp.route("/<int:lead_id>/convert", methods=["GET", "POST"])
@login_required
@require_permission("leads.convert")
def convert(lead_id):
    lead = _lead_or_403(lead_id)
    if lead.status != LeadStatus.WON:
        flash("لا يمكن التحويل قبل الوصول لحالة (ربحناها)", "error")
        return redirect(url_for("leads.detail", lead_id=lead.id))
    if lead.is_converted:
        flash("تم تحويل هذا العميل المحتمل مسبقاً", "info")
        return redirect(url_for("leads.detail", lead_id=lead.id))

    pms = _project_managers()
    if request.method == "POST":
        try:
            project = convert_lead_to_project(
                lead,
                project_name=request.form.get("project_name", ""),
                project_type=request.form.get("project_type", ""),
                manager_id=int(request.form.get("manager_id")),
                start_date=_parse_date(request.form.get("start_date")),
                end_date=_parse_date(request.form.get("end_date")),
                created_by_id=current_user.id,
                customer_id=lead.converted_customer_id or None,  # MARSOUD-CRM-P21
            )
            flash(f"تم تحويل العميل المحتمل إلى مشروع: {project.name}", "success")
            return redirect(url_for("projects.detail", project_id=project.id))
        except (CRMError, ValueError, TypeError) as e:
            flash(str(e), "error")
    return render_template("leads/convert.html",
                           lead=lead, pms=pms,
                           default_name=f"مشروع: {lead.service_needed} — {lead.client_name}",
                           default_type=lead.service_needed)


# ─── MARSOUD-CRM-STATUS-ACTIVITY-SPLIT (Abdelhamid 2026-07-15) ─────────
# When an activity outcome maps to a status change (e.g. MEETING with
# outcome "تم الاجتماع" → MEETING_SCHEDULED), the crm.activity_create
# route stores a suggestion in the session. These two endpoints let
# the user confirm or dismiss it — status is NEVER changed
# automatically.
@bp.route("/<int:lead_id>/suggest-status/apply", methods=["POST"])
@login_required
@require_permission("leads.manage")
def suggest_status_apply(lead_id):
    from flask import session
    lead = _lead_or_403(lead_id)
    suggestion = session.get("status_suggestion") or {}
    if (suggestion.get("lead_id") != lead.id
            or not suggestion.get("suggested")):
        flash("لا توجد اقتراح حالة صالح", "warning")
        return redirect(url_for("leads.detail", lead_id=lead.id))
    try:
        change_lead_status(
            lead, suggestion["suggested"],
            changed_by_id=current_user.id,
            note="اقتراح من نتيجة النشاط",
        )
        flash("تم تحديث حالة العميل حسب اقتراح النشاط", "success")
    except CRMError as e:
        flash(str(e), "error")
    session.pop("status_suggestion", None)
    return redirect(url_for("leads.detail", lead_id=lead.id))


@bp.route("/<int:lead_id>/suggest-status/dismiss", methods=["POST"])
@login_required
@require_permission("leads.manage")
def suggest_status_dismiss(lead_id):
    from flask import session
    lead = _lead_or_403(lead_id)
    session.pop("status_suggestion", None)
    flash("تم تجاهل اقتراح تغيير الحالة", "info")
    return redirect(url_for("leads.detail", lead_id=lead.id))
