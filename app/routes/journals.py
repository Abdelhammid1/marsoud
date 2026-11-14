from datetime import datetime, date
from flask import Blueprint, render_template, redirect, url_for, flash, request, g, send_file
from flask_login import login_required, current_user
from sqlalchemy import func, or_
from app import db
from app.models import (
    JournalEntry, JournalLine, Account, Customer, Vendor,
    JournalAudit, JournalAction, JournalTemplate, JournalTemplateLine,
    RecurringJournal, RecurrenceFrequency,
)
from app.services.ledger import post_journal, reverse_journal, LedgerError
from app.services.journals import (
    pause_entry, reactivate_entry, post_from_template,
)
from app.services.permissions import require_permission

bp = Blueprint("journals", __name__)


def _parse_date(s, default=None):
    if not s:
        return default
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return default


# MARSOUD-JOURNALS-LIST-P81 — list page helpers (Arabic type labels, safe ints, pager window)
_SOURCE_META = {
    "invoice": ("فاتورة مبيعات", "blue"),
    "invoice_cogs": ("تكلفة مبيعات", "blue"),
    "payment": ("تحصيل / دفعة", "green"),
    "treasury_receipt": ("إيصال خزينة", "green"),
    "money_transfer": ("تحويل أموال", "green"),
    "cash_custody": ("عهدة نقدية", "green"),
    "cash_count_adjustment": ("تسوية جرد نقدي", "green"),
    "capital_injection": ("زيادة رأس المال", "green"),
    "owner_drawings": ("مسحوبات المالك", "green"),
    "loan_short_receive": ("استلام قرض قصير", "green"),
    "loan_installment_paid": ("سداد قسط قرض", "green"),
    "vendor_bill": ("فاتورة مورد", "teal"),
    "vendor_bill_payment": ("سداد فاتورة مورد", "teal"),
    "vendor_payment": ("دفعة مورد", "teal"),
    "vendor_bill_refund": ("مرتجع مورد", "pink"),
    "refund": ("استرداد عميل", "pink"),
    "refund_cogs": ("تكلفة مرتجع", "pink"),
    "sales_commission_refund": ("استرداد عمولة", "pink"),
    "payroll": ("رواتب", "amber"),
    "payroll_settlement": ("تسوية رواتب", "amber"),
    "sales_commission": ("عمولة مبيعات", "amber"),
    "sales_commission_settlement": ("تسوية عمولات", "amber"),
    "sales_commission_settlement_reversal": ("عكس تسوية عمولات", "purple"),
    "commission_settle": ("تسوية عمولة", "amber"),
    "accrual_settle": ("تسوية مستحقات", "amber"),
    "opening_stock": ("مخزون افتتاحي", "slate"),
    "stock_adjustment": ("تسوية مخزون", "slate"),
    "asset_purchase": ("شراء أصل ثابت", "slate"),
    "opening_balance": ("رصيد افتتاحي", "slate"),
    "party_opening_balance": ("رصيد افتتاحي لطرف", "slate"),
}


def _source_meta(source_type, is_reversal=False):
    if is_reversal:
        return {"label": "عكسي", "tone": "purple"}
    if not source_type:
        return {"label": "يدوي", "tone": "slate"}
    label, tone = _SOURCE_META.get(source_type, (source_type, "slate"))
    return {"label": label, "tone": tone}


def _to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _page_window(page, total, span=2):
    nums = sorted({1, total} | set(range(max(1, page - span), min(total, page + span) + 1)))
    out, prev = [], 0
    for n in nums:
        if n - prev > 1:
            out.append(None)
        out.append(n)
        prev = n
    return out


@bp.route("/")
@login_required
@require_permission("journals.view")
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))

    # one filter builder shared with the export (see _build_filtered_query)
    q = _build_filtered_query(g.active_company.id, request.args)

    # Sort
    sort = request.args.get("sort", "date_desc")
    sort_map = {
        "date_desc": (JournalEntry.date.desc(), JournalEntry.id.desc()),
        "date_asc": (JournalEntry.date.asc(), JournalEntry.id.asc()),
        "number_desc": (JournalEntry.id.desc(),),
        "number_asc": (JournalEntry.id.asc(),),
    }
    for clause in sort_map.get(sort, sort_map["date_desc"]):
        q = q.order_by(clause)

    # Pagination (page is clamped to the valid range; bad input falls back to 1)
    per_page = 25
    total_count = q.count()
    total_pages = max((total_count + per_page - 1) // per_page, 1)
    page = min(max(_to_int(request.args.get("page"), 1), 1), total_pages)
    entries = q.offset((page - 1) * per_page).limit(per_page).all()
    range_from = (page - 1) * per_page + 1 if total_count else 0
    range_to = min(page * per_page, total_count)

    # Filtered totals (across ALL filtered rows, not just this page)
    total_debit_q = db.session.query(func.coalesce(func.sum(JournalLine.debit), 0)).filter(
        JournalLine.entry_id.in_(q.with_entities(JournalEntry.id).subquery())
    ).scalar()
    total_credit_q = db.session.query(func.coalesce(func.sum(JournalLine.credit), 0)).filter(
        JournalLine.entry_id.in_(q.with_entities(JournalEntry.id).subquery())
    ).scalar()

    accounts = Account.query.filter_by(
        company_id=g.active_company.id, is_active=True
    ).order_by(Account.code).all()

    # Users who have ever created an entry in this company — for the user filter dropdown
    from app.models import User as UserModel
    user_ids = db.session.query(JournalEntry.created_by).filter(
        JournalEntry.company_id == g.active_company.id,
        JournalEntry.created_by.isnot(None),
    ).distinct().all()
    users = UserModel.query.filter(UserModel.id.in_([u[0] for u in user_ids])).all()

    # Type dropdown: only the source types this company really has, in Arabic
    present_types = [r[0] for r in db.session.query(JournalEntry.source_type).filter(
        JournalEntry.company_id == g.active_company.id,
        JournalEntry.source_type.isnot(None),
    ).distinct().all()]
    source_options = sorted(
        ((t, _source_meta(t)["label"]) for t in present_types), key=lambda x: x[1]
    )

    # Current filters (without page/fmt) so pager + export links keep them
    qargs = {k: v for k, v in request.args.items() if v and k not in ("page", "fmt")}
    adv_keys = ("amount_min", "amount_max", "source_type", "status",
                "account_id", "reference", "user_id")
    adv_count = sum(1 for k in adv_keys if qargs.get(k))
    active_filters = sum(1 for k in qargs if k != "sort")

    ctx = dict(
        entries=entries,
        total_count=total_count, page=page, total_pages=total_pages,
        range_from=range_from, range_to=range_to,
        page_window=_page_window(page, total_pages),
        total_debit=float(total_debit_q or 0),
        total_credit=float(total_credit_q or 0),
        sort=sort, qargs=qargs, active_filters=active_filters,
        source_meta=_source_meta,
    )

    # HTMX partial: only the results fragment. History-restore requests (Back
    # button after a cache miss) need the full page, not the fragment.
    if request.headers.get("HX-Request") and not request.headers.get("HX-History-Restore-Request"):
        return render_template("journals/_results.html", **ctx)

    return render_template(
        "journals/index.html",
        accounts=accounts, users=users,
        source_options=source_options, adv_count=adv_count,
        **ctx,
    )



@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("journals.create")
def new():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    accounts = Account.query.filter_by(company_id=g.active_company.id, is_active=True).order_by(Account.code).all()
    templates = JournalTemplate.query.filter_by(
        company_id=g.active_company.id, is_active=True
    ).order_by(JournalTemplate.name).all()

    if request.method == "POST":
        description = request.form.get("description", "").strip()
        reference = request.form.get("reference", "").strip() or None
        entry_date = _parse_date(request.form.get("date"), date.today())
        currency = request.form.get("currency", g.active_company.base_currency)
        exchange_rate = float(request.form.get("exchange_rate", 1.0))

        account_ids = request.form.getlist("account_id[]")
        debits = request.form.getlist("debit[]")
        credits = request.form.getlist("credit[]")
        memos = request.form.getlist("memo[]")
        # MARSOUD-COST-CENTERS-01 — optional per-line classifier.
        cost_center_ids = request.form.getlist("cost_center_id[]")

        lines = []
        for i, aid in enumerate(account_ids):
            if not aid:
                continue
            d = float(debits[i] or 0)
            c = float(credits[i] or 0)
            if d == 0 and c == 0:
                continue
            cc_raw = cost_center_ids[i] if i < len(cost_center_ids) else ""
            lines.append({
                "account_id": int(aid), "debit": d, "credit": c,
                "memo": memos[i] if i < len(memos) else None,
                "cost_center_id": int(cc_raw) if cc_raw else None,
            })

        cashflow_category = request.form.get("cashflow_category") or None
        try:
            entry = post_journal(
                company_id=g.active_company.id,
                description=description, lines=lines,
                entry_date=entry_date, reference=reference,
                currency=currency, exchange_rate=exchange_rate,
                created_by=current_user.id,
                cashflow_category=cashflow_category,
            )
            db.session.add(JournalAudit(
                entry_id=entry.id, user_id=current_user.id,
                action=JournalAction.CREATED, reason="إنشاء يدوي",
            ))
            db.session.commit()
            flash(f"تم تسجيل القيد {entry.number}", "success")
            return redirect(url_for("journals.view", entry_id=entry.id))
        except LedgerError as e:
            flash(str(e), "error")

    from app.models import CostCenter
    cost_centers = (CostCenter.query
                     .filter_by(company_id=g.active_company.id,
                                 is_active=True)
                     .filter(CostCenter.deleted_at.is_(None))
                     .order_by(CostCenter.code).all())
    return render_template("journals/form.html",
                            accounts=accounts, templates=templates,
                            cost_centers=cost_centers)


@bp.route("/<int:entry_id>")
@login_required
@require_permission("journals.view")
def view(entry_id):
    entry = db.session.get(JournalEntry, entry_id)
    if not entry or entry.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("journals.index"))

    # Resolve source link if available
    source_link = None
    source_label = None
    if entry.source_type == "invoice" and entry.source_id:
        from app.models import Invoice
        inv = db.session.get(Invoice, entry.source_id)
        if inv and inv.company_id == g.active_company.id:
            source_link = url_for("invoices.view", invoice_id=inv.id)
            source_label = f"فاتورة {inv.number}"
    elif entry.source_type == "payment" and entry.source_id:
        from app.models import Invoice
        inv = db.session.get(Invoice, entry.source_id)
        if inv and inv.company_id == g.active_company.id:
            source_link = url_for("invoices.view", invoice_id=inv.id)
            source_label = f"دفعة على فاتورة {inv.number}"
    elif entry.source_type == "payroll" and entry.source_id:
        from app.models import PayrollRun
        run = db.session.get(PayrollRun, entry.source_id)
        if run and run.company_id == g.active_company.id:
            source_link = url_for("payroll.view_run", run_id=run.id)
            source_label = f"كشف رواتب {run.number or ''}"

    audits = JournalAudit.query.filter_by(entry_id=entry.id).order_by(JournalAudit.created_at).all()
    return render_template(
        "journals/view.html",
        entry=entry, source_link=source_link, source_label=source_label, audits=audits,
    )


@bp.route("/<int:entry_id>/reverse", methods=["POST"])
@login_required
@require_permission("journals.reverse")
def reverse(entry_id):
    entry = db.session.get(JournalEntry, entry_id)
    if not entry or entry.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("journals.index"))
    try:
        new_entry = reverse_journal(entry_id, created_by=current_user.id)
        db.session.add(JournalAudit(
            entry_id=entry.id, user_id=current_user.id,
            action=JournalAction.REVERSED, reason=f"تم إنشاء قيد عكسي {new_entry.number}",
        ))
        db.session.commit()
        flash(f"تم إنشاء قيد عكسي {new_entry.number}", "success")
    except LedgerError as e:
        flash(str(e), "error")
    return redirect(url_for("journals.view", entry_id=entry_id))


@bp.route("/<int:entry_id>/pause", methods=["POST"])
@login_required
@require_permission("journals.pause")
def pause(entry_id):
    entry = db.session.get(JournalEntry, entry_id)
    if not entry or entry.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("journals.index"))
    try:
        pause_entry(entry, request.form.get("reason", ""), current_user.id)
        flash("تم إيقاف القيد — لا يؤثر على التقارير", "success")
    except LedgerError as e:
        flash(str(e), "error")
    return redirect(url_for("journals.view", entry_id=entry_id))


@bp.route("/<int:entry_id>/reactivate", methods=["POST"])
@login_required
@require_permission("journals.pause")
def reactivate(entry_id):
    entry = db.session.get(JournalEntry, entry_id)
    if not entry or entry.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("journals.index"))
    try:
        reactivate_entry(entry, request.form.get("reason", ""), current_user.id)
        flash("تم إعادة تنشيط القيد — التقارير محدّثة", "success")
    except LedgerError as e:
        flash(str(e), "error")
    return redirect(url_for("journals.view", entry_id=entry_id))


@bp.route("/templates")
@login_required
@require_permission("journals.view")
def templates_list():
    templates = JournalTemplate.query.filter_by(company_id=g.active_company.id).all()
    return render_template("journals/templates.html", templates=templates)


@bp.route("/templates/new", methods=["GET", "POST"])
@login_required
@require_permission("journals.create")
def templates_new():
    accounts = Account.query.filter_by(
        company_id=g.active_company.id, is_active=True
    ).order_by(Account.code).all()
    if request.method == "POST":
        try:
            tpl = JournalTemplate(
                company_id=g.active_company.id,
                name=request.form.get("name", "").strip(),
                description=request.form.get("description", ""),
            )
            if not tpl.name:
                raise ValueError("اسم القالب مطلوب")
            db.session.add(tpl)
            db.session.flush()
            account_ids = request.form.getlist("account_id[]")
            debits = request.form.getlist("debit[]")
            credits = request.form.getlist("credit[]")
            memos = request.form.getlist("memo[]")
            for i, aid in enumerate(account_ids):
                if not aid:
                    continue
                d, c = float(debits[i] or 0), float(credits[i] or 0)
                if d == 0 and c == 0:
                    continue
                db.session.add(JournalTemplateLine(
                    template_id=tpl.id, account_id=int(aid),
                    debit=d, credit=c,
                    memo=memos[i] if i < len(memos) else None,
                ))
            db.session.commit()
            flash("تم حفظ القالب", "success")
            return redirect(url_for("journals.templates_list"))
        except ValueError as e:
            flash(str(e), "error")
    return render_template("journals/template_form.html", accounts=accounts)


@bp.route("/templates/<int:template_id>/use")
@login_required
@require_permission("journals.create")
def use_template(template_id):
    tpl = db.session.get(JournalTemplate, template_id)
    if not tpl or tpl.company_id != g.active_company.id:
        return redirect(url_for("journals.templates_list"))
    accounts = Account.query.filter_by(
        company_id=g.active_company.id, is_active=True
    ).order_by(Account.code).all()
    templates = JournalTemplate.query.filter_by(company_id=g.active_company.id, is_active=True).all()
    return render_template("journals/form.html", accounts=accounts, templates=templates, prefill_template=tpl)


@bp.route("/recurring")
@login_required
@require_permission("journals.view")
def recurring_list():
    items = RecurringJournal.query.filter_by(
        company_id=g.active_company.id, is_deleted=False
    ).order_by(RecurringJournal.is_active.desc(), RecurringJournal.next_run_date).all()
    return render_template("journals/recurring.html", items=items)


@bp.route("/recurring/<int:rec_id>/edit", methods=["POST"])
@login_required
@require_permission("journals.recurring")
def recurring_edit(rec_id):
    from app.models import RecurringAction
    from app.services.journals import log_recurring_action
    r = db.session.get(RecurringJournal, rec_id)
    if not r or r.company_id != g.active_company.id or r.is_deleted:
        flash("غير موجود", "error")
        return redirect(url_for("journals.recurring_list"))
    changes = []
    new_name = request.form.get("name", "").strip()
    if new_name and new_name != r.name:
        changes.append(f"الاسم: {r.name} → {new_name}")
        r.name = new_name
    try:
        new_freq = RecurrenceFrequency[request.form.get("frequency", r.frequency.name)]
        if new_freq != r.frequency:
            changes.append(f"التكرار: {r.frequency.name} → {new_freq.name}")
            r.frequency = new_freq
    except KeyError:
        pass
    new_next = _parse_date(request.form.get("next_run_date"), r.next_run_date)
    if new_next != r.next_run_date:
        changes.append(f"التاريخ التالي: {r.next_run_date} → {new_next}")
        r.next_run_date = new_next
    new_end = _parse_date(request.form.get("end_date"), r.end_date)
    if new_end != r.end_date:
        changes.append(f"تاريخ النهاية: {r.end_date} → {new_end}")
        r.end_date = new_end
    if changes:
        log_recurring_action(r, RecurringAction.EDIT, current_user.id,
                             reason=" · ".join(changes))
    db.session.commit()
    flash("تم تعديل الجدول", "success")
    return redirect(url_for("journals.recurring_list"))


@bp.route("/recurring/<int:rec_id>/stop", methods=["POST"])
@login_required
@require_permission("journals.recurring")
def recurring_stop(rec_id):
    from app.models import RecurringAction
    from app.services.journals import log_recurring_action
    r = db.session.get(RecurringJournal, rec_id)
    if not r or r.company_id != g.active_company.id or r.is_deleted:
        flash("غير موجود", "error")
        return redirect(url_for("journals.recurring_list"))
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("السبب مطلوب", "error")
        return redirect(url_for("journals.recurring_list"))
    r.is_active = False
    log_recurring_action(r, RecurringAction.STOP, current_user.id, reason=reason)
    db.session.commit()
    flash("تم إيقاف الجدول", "success")
    return redirect(url_for("journals.recurring_list"))


@bp.route("/recurring/<int:rec_id>/resume", methods=["POST"])
@login_required
@require_permission("journals.recurring")
def recurring_resume(rec_id):
    from app.models import RecurringAction
    from app.services.journals import log_recurring_action
    r = db.session.get(RecurringJournal, rec_id)
    if not r or r.company_id != g.active_company.id or r.is_deleted:
        flash("غير موجود", "error")
        return redirect(url_for("journals.recurring_list"))
    r.is_active = True
    log_recurring_action(r, RecurringAction.RESUME, current_user.id,
                         reason=request.form.get("reason"))
    db.session.commit()
    flash("تم تفعيل الجدول", "success")
    return redirect(url_for("journals.recurring_list"))


@bp.route("/recurring/<int:rec_id>/delete", methods=["POST"])
@login_required
@require_permission("journals.recurring")
def recurring_delete(rec_id):
    from app.models import RecurringAction
    from app.services.journals import log_recurring_action
    r = db.session.get(RecurringJournal, rec_id)
    if not r or r.company_id != g.active_company.id or r.is_deleted:
        flash("غير موجود", "error")
        return redirect(url_for("journals.recurring_list"))
    reason = (request.form.get("reason") or "حذف").strip()
    r.is_active = False
    r.is_deleted = True
    log_recurring_action(r, RecurringAction.DELETE, current_user.id, reason=reason)
    db.session.commit()
    flash("تم حذف الجدول", "success")
    return redirect(url_for("journals.recurring_list"))


@bp.route("/recurring/<int:rec_id>/log")
@login_required
@require_permission("journals.view")
def recurring_log(rec_id):
    r = db.session.get(RecurringJournal, rec_id)
    if not r or r.company_id != g.active_company.id:
        flash("غير موجود", "error")
        return redirect(url_for("journals.recurring_list"))
    logs = sorted(r.logs, key=lambda l: l.created_at, reverse=True)
    return render_template("journals/recurring_log.html", schedule=r, logs=logs)


@bp.route("/recurring/new", methods=["GET", "POST"])
@login_required
@require_permission("journals.recurring")
def recurring_new():
    templates = JournalTemplate.query.filter_by(
        company_id=g.active_company.id, is_active=True
    ).all()
    if request.method == "POST":
        try:
            r = RecurringJournal(
                company_id=g.active_company.id,
                template_id=int(request.form.get("template_id")),
                name=request.form.get("name", "").strip(),
                frequency=RecurrenceFrequency[request.form.get("frequency")],
                next_run_date=_parse_date(request.form.get("next_run_date"), date.today()),
                end_date=_parse_date(request.form.get("end_date")),
            )
            if not r.name:
                raise ValueError("الاسم مطلوب")
            db.session.add(r)
            db.session.commit()
            flash("تم إنشاء الجدول التلقائي", "success")
            return redirect(url_for("journals.recurring_list"))
        except (ValueError, KeyError) as e:
            flash(str(e), "error")
    return render_template("journals/recurring_form.html", templates=templates)


def _build_filtered_query(company_id, args):
    """Single source of truth for the list page AND the Excel/PDF export, so
    what you export is exactly what you see (search, amount, account, user,
    reference, dates, type, status)."""
    q = JournalEntry.query.filter_by(company_id=company_id)

    # Search: number / description / reference / amount / customer / vendor
    search = (args.get("search") or "").strip()
    if search:
        like = f"%{search}%"
        amount_search = None
        try:
            amount_search = float(search)
        except ValueError:
            pass

        from app.models import Invoice
        customer_match = db.session.query(Invoice.id).join(
            Customer, Invoice.customer_id == Customer.id
        ).filter(Customer.name.ilike(like)).subquery()
        vendor_match = db.session.query(Vendor.id).filter(Vendor.name.ilike(like)).subquery()

        clauses = [
            JournalEntry.number.ilike(like),
            JournalEntry.description.ilike(like),
            JournalEntry.reference.ilike(like),
            db.and_(
                JournalEntry.source_type.in_(["invoice", "payment", "refund", "credit_note"]),
                JournalEntry.source_id.in_(customer_match),
            ),
            db.and_(
                JournalEntry.source_type == "vendor_payment",
                JournalEntry.source_id.in_(vendor_match),
            ),
        ]
        if amount_search is not None:
            matching_ids = db.session.query(JournalLine.entry_id).filter(
                or_(JournalLine.debit == amount_search, JournalLine.credit == amount_search)
            ).distinct().subquery()
            clauses.append(JournalEntry.id.in_(matching_ids))
        q = q.filter(or_(*clauses))

    # Amount range (any line within the range)
    amount_min = args.get("amount_min")
    amount_max = args.get("amount_max")
    if amount_min or amount_max:
        line_q = db.session.query(JournalLine.entry_id).distinct()
        if amount_min:
            try:
                amn = float(amount_min)
                line_q = line_q.filter(or_(JournalLine.debit >= amn, JournalLine.credit >= amn))
            except ValueError:
                pass
        if amount_max:
            try:
                amx = float(amount_max)
                line_q = line_q.filter(or_(
                    db.and_(JournalLine.debit > 0, JournalLine.debit <= amx),
                    db.and_(JournalLine.credit > 0, JournalLine.credit <= amx),
                ))
            except ValueError:
                pass
        q = q.filter(JournalEntry.id.in_(line_q.subquery()))

    sd = _parse_date(args.get("start_date"))
    ed = _parse_date(args.get("end_date"))
    if sd:
        q = q.filter(JournalEntry.date >= sd)
    if ed:
        q = q.filter(JournalEntry.date <= ed)

    source_type = args.get("source_type")
    if source_type == "manual":
        q = q.filter(JournalEntry.source_type.is_(None))
    elif source_type == "reversal":
        q = q.filter(JournalEntry.is_reversal.is_(True))
    elif source_type:
        q = q.filter(JournalEntry.source_type == source_type)

    status = args.get("status")
    if status == "active":
        q = q.filter(JournalEntry.is_active.is_(True))
    elif status == "paused":
        q = q.filter(JournalEntry.is_active.is_(False))

    account_id = _to_int(args.get("account_id"))
    if account_id:
        matching = db.session.query(JournalLine.entry_id).filter(
            JournalLine.account_id == account_id
        ).distinct().subquery()
        q = q.filter(JournalEntry.id.in_(matching))

    user_id = _to_int(args.get("user_id"))
    if user_id:
        q = q.filter(JournalEntry.created_by == user_id)

    reference_prefix = (args.get("reference") or "").strip()
    if reference_prefix:
        q = q.filter(JournalEntry.reference.ilike(f"{reference_prefix}%"))

    return q


@bp.route("/export")
@login_required
@require_permission("journals.view")
def export_filtered():
    """Export the currently filtered list as Excel or PDF (fmt=pdf|excel)."""
    fmt = request.args.get("fmt", "excel")
    q = _build_filtered_query(g.active_company.id, request.args)
    entries = q.order_by(JournalEntry.date.desc()).all()

    if fmt == "pdf":
        from app.services.export import export_journals_list_pdf
        period = ""
        if request.args.get("start_date") or request.args.get("end_date"):
            period = f"{request.args.get('start_date', '')} → {request.args.get('end_date', '')}"
        buf = export_journals_list_pdf(g.active_company, entries, period_label=period)
        return send_file(buf, mimetype="application/pdf",
                         as_attachment=True, download_name=f"journals-{date.today()}.pdf")

    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "قيود اليومية"
    # MARSOUD-JOURNAL-EXPORT-AR (Batch 6 Ticket 5, 2026-07-29) —
    # flip the workbook right-to-left so headers + first-column
    # data land where an Arabic reader expects them.
    ws.sheet_view.rightToLeft = True
    headers = ["الرقم", "التاريخ", "الوصف", "المرجع", "المدين", "الدائن", "الحالة"]
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="0A2540")
    row = 2
    for e in entries:
        ws.cell(row=row, column=1, value=e.number)
        ws.cell(row=row, column=2, value=str(e.date))
        ws.cell(row=row, column=3, value=e.description)
        ws.cell(row=row, column=4, value=e.reference or "")
        ws.cell(row=row, column=5, value=float(e.total_debit)).number_format = "#,##0.00"
        ws.cell(row=row, column=6, value=float(e.total_credit)).number_format = "#,##0.00"
        ws.cell(row=row, column=7, value="نشط" if e.is_active else "موقوف")
        row += 1

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=f"journals-{date.today()}.xlsx")


@bp.route("/<int:entry_id>/export/<fmt>")
@login_required
@require_permission("journals.view")
def export_entry(entry_id, fmt):
    entry = db.session.get(JournalEntry, entry_id)
    if not entry or entry.company_id != g.active_company.id:
        return redirect(url_for("journals.index"))
    if fmt == "pdf":
        from app.services.export import export_journal_entry_pdf
        buf = export_journal_entry_pdf(entry)
        return send_file(buf, mimetype="application/pdf",
                         download_name=f"{entry.number or 'entry-' + str(entry.id)}.pdf",
                         as_attachment=True)
    if fmt == "excel":
        from app.services.export import export_journal_entry_excel
        buf = export_journal_entry_excel(entry)
        return send_file(buf,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         download_name=f"{entry.number or 'entry-' + str(entry.id)}.xlsx",
                         as_attachment=True)
    return redirect(url_for("journals.view", entry_id=entry.id))


@bp.route("/bulk", methods=["POST"])
@login_required
@require_permission("journals.pause")
def bulk_action():
    """Handle bulk actions on selected entries: export, pause."""
    action = request.form.get("action")
    selected_ids = request.form.getlist("entry_ids[]")
    if not selected_ids:
        flash("لم يتم تحديد أي قيد", "warning")
        return redirect(url_for("journals.index"))

    entries = JournalEntry.query.filter(
        JournalEntry.id.in_([int(i) for i in selected_ids]),
        JournalEntry.company_id == g.active_company.id,
    ).all()

    if action == "export_excel":
        import io
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        wb = Workbook()
        ws = wb.active
        ws.title = "قيود مختارة"
        # MARSOUD-JOURNAL-EXPORT-AR (Batch 6 Ticket 5, 2026-07-29).
        ws.sheet_view.rightToLeft = True
        for col, h in enumerate(["الرقم", "التاريخ", "الوصف", "المرجع", "المدين", "الدائن", "الحالة"], 1):
            c = ws.cell(row=1, column=col, value=h)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="0A2540")
        row = 2
        for e in entries:
            ws.cell(row=row, column=1, value=e.number)
            ws.cell(row=row, column=2, value=str(e.date))
            ws.cell(row=row, column=3, value=e.description)
            ws.cell(row=row, column=4, value=e.reference or "")
            ws.cell(row=row, column=5, value=float(e.total_debit)).number_format = "#,##0.00"
            ws.cell(row=row, column=6, value=float(e.total_credit)).number_format = "#,##0.00"
            ws.cell(row=row, column=7, value="نشط" if e.is_active else "موقوف")
            row += 1
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"selected-journals-{date.today()}.xlsx")

    if action == "export_pdf":
        from app.services.export import export_journals_list_pdf
        buf = export_journals_list_pdf(g.active_company, entries,
                                       period_label=f"{len(entries)} selected entries")
        return send_file(buf, mimetype="application/pdf",
                         as_attachment=True, download_name=f"selected-journals-{date.today()}.pdf")

    if action == "pause":
        reason = (request.form.get("bulk_reason") or "").strip()
        if not reason:
            flash("سبب الإيقاف مطلوب للإيقاف الجماعي", "error")
            return redirect(url_for("journals.index"))
        paused_count = 0
        for e in entries:
            try:
                pause_entry(e, reason, current_user.id)
                paused_count += 1
            except LedgerError:
                pass
        flash(f"تم إيقاف {paused_count} قيد من أصل {len(entries)}", "success")
        return redirect(url_for("journals.index"))

    flash("إجراء غير معروف", "error")
    return redirect(url_for("journals.index"))
