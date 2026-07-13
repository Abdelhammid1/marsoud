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
    pipeline_leads = [l for l in leads if l.status != LeadStatus.NO_RESPONSE]

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
    pipeline_statuses = [s for s in LeadStatus if s != LeadStatus.NO_RESPONSE]
    columns = {s: [] for s in pipeline_statuses}
    for l in pipeline_leads:
        columns[l.status].append(l)

    return render_template(
        "leads/index.html",
        leads=pipeline_leads,
        statuses=LeadStatus,               # for the filter dropdown
        pipeline_statuses=pipeline_statuses,  # for the Kanban columns
        status_counts=status_counts,
        no_response_count=no_response_count,
        reps=_sales_reps() if _can_view_all_leads() else [],
        campaigns=_active_campaigns(),
        q=q, status_filter=status_filter, rep_filter=rep_filter,
        campaign_filter=campaign_filter,
        date_from=date_from, date_to=date_to,
        view=view, columns=columns,
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
    buf = export_leads_excel(g.active_company, leads)
    fname = f"leads-{g.active_company.id}-{datetime.now():%Y%m%d-%H%M}.xlsx"
    return send_file(
        buf, as_attachment=True, download_name=fname,
        mimetype=("application/vnd.openxmlformats-officedocument."
                  "spreadsheetml.sheet"),
    )


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
            lead = Lead(
                company_id=g.active_company.id,
                number=_lead_number,
                client_name=request.form.get("client_name", "").strip(),
                phone=request.form.get("phone", "").strip(),
                email=(request.form.get("email") or "").strip().lower() or None,
                service_needed=request.form.get("service_needed", "").strip(),
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
                raise CRMError("الاسم والهاتف والخدمة حقول مطلوبة")
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
    return render_template("leads/form.html", lead=None,
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
    return render_template("leads/detail.html",
                           lead=lead, statuses=LeadStatus,
                           lead_types=LeadType, lead_sources=LeadSource,
                           can_manage_files=can_manage_files,
                           activities=activities, contacts=contacts,
                           activity_types=LeadActivityType)


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
            lead.client_name = request.form.get("client_name", lead.client_name).strip()
            lead.phone = request.form.get("phone", lead.phone).strip()
            lead.email = (request.form.get("email") or "").strip().lower() or None
            lead.service_needed = request.form.get("service_needed", lead.service_needed).strip()
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
    return render_template("leads/form.html", lead=lead, reps=_sales_reps(),
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
    if kind not in ("quotation", "contract"):
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
        else:
            lead.contract_path = doc.file_path
        db.session.commit()
        flash(f"تم رفع {'عرض السعر' if kind == 'quotation' else 'العقد'}", "success")
    except DocumentError as e:
        flash(str(e), "error")
    return redirect(url_for("leads.detail", lead_id=lead.id))


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
                link_url=(
                    url_for("leads.detail", lead_id=lead.id) + "#comments"
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
            )
            flash(f"تم تحويل العميل المحتمل إلى مشروع: {project.name}", "success")
            return redirect(url_for("projects.detail", project_id=project.id))
        except (CRMError, ValueError, TypeError) as e:
            flash(str(e), "error")
    return render_template("leads/convert.html",
                           lead=lead, pms=pms,
                           default_name=f"مشروع: {lead.service_needed} — {lead.client_name}",
                           default_type=lead.service_needed)
