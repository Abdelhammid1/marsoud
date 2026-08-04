from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request, g, jsonify, abort
from flask_login import login_required
from app import db
from app.models import Account, AccountType, NormalSide, JournalEntry, JournalLine
from app.models.account import NORMAL_SIDE_FOR_TYPE
from app.services.permissions import require_permission
from app.services.source_reference import resolve_reference


TYPE_DEFAULT_CODES = {
    "ASSET": 1000, "LIABILITY": 2000, "EQUITY": 3000,
    "REVENUE": 4000, "EXPENSE": 5000,
}


def _trailing_zeros(s):
    n = 0
    for ch in reversed(s):
        if ch == "0":
            n += 1
        else:
            break
    return n


def _suggest_next_code(company_id, type_str=None, parent_id=None):
    """Suggest next account code following the hierarchy step rule:
    step = 10 ^ max(trailing_zeros(parent.code) - 1, 0)
    """
    if parent_id:
        parent = db.session.get(Account, int(parent_id))
        if not parent or parent.company_id != company_id:
            return None
        if not parent.code.isdigit():
            return None
        trailing = _trailing_zeros(parent.code)
        step = 10 ** max(trailing - 1, 0) if trailing > 0 else 1
        children_codes = [
            int(c.code) for c in Account.query.filter_by(
                company_id=company_id, parent_id=parent.id
            ).all() if c.code.isdigit()
        ]
        new_code = (max(children_codes) + step) if children_codes else (int(parent.code) + step)
    else:
        default = TYPE_DEFAULT_CODES.get(type_str)
        if not default:
            return None
        try:
            acc_type = AccountType[type_str]
        except KeyError:
            return None
        root_codes = [
            int(r.code) for r in Account.query.filter_by(
                company_id=company_id, parent_id=None, type=acc_type
            ).all() if r.code.isdigit()
        ]
        new_code = (max(root_codes) + 1000) if root_codes else default
        step = 1000

    while Account.query.filter_by(company_id=company_id, code=str(new_code)).first():
        new_code += step

    return str(new_code)

bp = Blueprint("accounts", __name__)


@bp.route("/")
@login_required
def index():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    accounts = Account.query.filter_by(company_id=g.active_company.id).order_by(Account.code).all()
    # build tree
    by_id = {a.id: a for a in accounts}
    tree = []
    children_map = {}
    for a in accounts:
        children_map.setdefault(a.parent_id, []).append(a)
    roots = children_map.get(None, [])
    return render_template("accounts/index.html", accounts=accounts, roots=roots, children_map=children_map)


@bp.route("/suggest-code")
@login_required
def suggest_code():
    if not g.active_company:
        return jsonify({"code": ""})
    parent_id = request.args.get("parent_id") or None
    type_str = request.args.get("type") or None
    code = _suggest_next_code(g.active_company.id, type_str=type_str, parent_id=parent_id)
    # If parent given, also return its type so the form can lock to it
    parent_type = None
    if parent_id:
        parent = db.session.get(Account, int(parent_id))
        if parent and parent.company_id == g.active_company.id:
            parent_type = parent.type.name
    return jsonify({"code": code or "", "parent_type": parent_type})


@bp.route("/new", methods=["GET", "POST"])
@login_required
@require_permission("accounts.manage")
def new():
    if not g.active_company:
        return redirect(url_for("companies.new"))
    parents = Account.query.filter_by(
        company_id=g.active_company.id, is_active=True
    ).order_by(Account.code).all()

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        name = request.form.get("name", "").strip()
        name_ar = request.form.get("name_ar", "").strip()
        type_str = request.form.get("type")
        parent_id = request.form.get("parent_id") or None
        if parent_id == "":
            parent_id = None

        # If a parent is selected, type is derived from the parent — guarantees consistency
        if parent_id:
            parent = db.session.get(Account, int(parent_id))
            if parent and parent.company_id == g.active_company.id:
                type_str = parent.type.name

        try:
            acc_type = AccountType[type_str]
        except (KeyError, TypeError):
            flash("نوع الحساب غير صحيح", "error")
            return render_template("accounts/form.html", parents=parents, account_types=AccountType)

        # Auto-fill code if user left it blank
        if not code:
            code = _suggest_next_code(g.active_company.id, type_str=type_str, parent_id=parent_id)
            if not code:
                flash("تعذّر توليد الكود تلقائياً — أدخله يدوياً", "error")
                return render_template("accounts/form.html", parents=parents, account_types=AccountType)

        if Account.query.filter_by(company_id=g.active_company.id, code=code).first():
            flash(f"الكود {code} مستخدم بالفعل", "error")
            return render_template("accounts/form.html", parents=parents, account_types=AccountType)

        acc = Account(
            company_id=g.active_company.id,
            code=code,
            name=name,
            name_ar=name_ar,
            type=acc_type,
            normal_side=NORMAL_SIDE_FOR_TYPE[acc_type],
            parent_id=int(parent_id) if parent_id else None,
        )
        db.session.add(acc)
        db.session.commit()
        flash(f"تم إضافة الحساب {acc.code}", "success")
        return redirect(url_for("accounts.index"))

    return render_template("accounts/form.html", parents=parents, account_types=AccountType)


def _descendants(account):
    """All descendants of an account (depth-first), excluding the account itself."""
    out = []
    stack = list(account.children)
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.children)
    return out


def _entry_count(account, descendants):
    """Number of journal lines posted to the account or any of its descendants."""
    ids = [account.id] + [d.id for d in descendants]
    from app.models.journal import JournalLine
    return JournalLine.query.filter(JournalLine.account_id.in_(ids)).count()


@bp.route("/<int:account_id>/edit", methods=["GET", "POST"])
@login_required
@require_permission("accounts.manage")
def edit(account_id):
    if not g.active_company:
        return redirect(url_for("companies.new"))
    acc = db.session.get(Account, account_id)
    if not acc or acc.company_id != g.active_company.id:
        flash("غير مسموح", "error")
        return redirect(url_for("accounts.index"))

    descendants = _descendants(acc)
    descendant_ids = {d.id for d in descendants}
    entry_count = _entry_count(acc, descendants)

    # Parent options: any account in the company except self and its own descendants
    # (prevents creating a cycle).
    parents = [
        p for p in Account.query.filter_by(company_id=g.active_company.id)
        .order_by(Account.code).all()
        if p.id != acc.id and p.id not in descendant_ids
    ]

    def render_form():
        return render_template(
            "accounts/edit.html", account=acc, parents=parents,
            account_types=AccountType, normal_sides=NormalSide,
            entry_count=entry_count, descendant_count=len(descendants),
        )

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        name = request.form.get("name", "").strip()
        name_ar = request.form.get("name_ar", "").strip()
        type_str = request.form.get("type")
        normal_str = request.form.get("normal_side")
        parent_id = request.form.get("parent_id") or None
        if parent_id == "":
            parent_id = None

        # Validate parent: must belong to company, not self, not a descendant.
        parent = None
        if parent_id:
            parent = db.session.get(Account, int(parent_id))
            if not parent or parent.company_id != g.active_company.id:
                flash("الحساب الأب غير صحيح", "error")
                return render_form()
            if parent.id == acc.id or parent.id in descendant_ids:
                flash("لا يمكن جعل الحساب تابعاً لنفسه أو لأحد أبنائه", "error")
                return render_form()
            # Parent picks the classification — guarantees hierarchy consistency.
            type_str = parent.type.name

        try:
            acc_type = AccountType[type_str]
        except (KeyError, TypeError):
            flash("نوع الحساب غير صحيح", "error")
            return render_form()

        try:
            normal_side = NormalSide[normal_str] if normal_str else NORMAL_SIDE_FOR_TYPE[acc_type]
        except (KeyError, TypeError):
            normal_side = NORMAL_SIDE_FOR_TYPE[acc_type]

        if not code:
            flash("الكود مطلوب", "error")
            return render_form()
        clash = Account.query.filter(
            Account.company_id == g.active_company.id,
            Account.code == code,
            Account.id != acc.id,
        ).first()
        if clash:
            flash(f"الكود {code} مستخدم بالفعل — اختر كوداً آخر", "error")
            return render_form()
        if not name:
            flash("الاسم بالإنجليزية مطلوب", "error")
            return render_form()

        # If the account has posted entries, require explicit confirmation.
        if entry_count > 0 and not request.form.get("confirm"):
            flash(f"هذا الحساب (أو أبناؤه) عليه {entry_count} قيد — أكّد التعديل", "warning")
            return render_form()

        type_changed = acc.type != acc_type

        acc.code = code
        acc.name = name
        acc.name_ar = name_ar
        acc.type = acc_type
        acc.normal_side = normal_side
        acc.parent_id = parent.id if parent else None

        # Classification change cascades to all descendants (keeps tree consistent).
        if type_changed:
            for d in descendants:
                d.type = acc_type
                d.normal_side = NORMAL_SIDE_FOR_TYPE[acc_type]

        db.session.commit()
        try:
            from app.services.superadmin import log_platform_action
            log_platform_action("account_edited",
                                target_company_id=acc.company_id,
                                actor_id=current_user.id,
                                details=f"#{acc.code} — {acc.name}")
        except Exception:
            pass
        flash(f"تم تعديل الحساب {acc.code}", "success")
        return redirect(url_for("accounts.index"))

    return render_form()


@bp.route("/<int:account_id>/delete", methods=["POST"])
@login_required
@require_permission("accounts.manage")
def delete(account_id):
    acc = db.session.get(Account, account_id)
    if not acc or acc.company_id != g.active_company.id:
        flash("غير مسموح", "error")
        return redirect(url_for("accounts.index"))
    if acc.lines.count() > 0:
        flash("لا يمكن حذف حساب له قيود — تم تعطيله بدلاً من ذلك", "warning")
        acc.is_active = False
        db.session.commit()
    else:
        db.session.delete(acc)
        db.session.commit()
        flash("تم الحذف", "success")
    return redirect(url_for("accounts.index"))


# ─── MARSOUD-55: account ledger (detailed account movement) ────────────
def _resolve_source(entry):
    """Return (label_ar, link_url_or_None) for a JournalEntry's source.

    MARSOUD-SOURCE-LABEL-UNIFY (2026-08-04) — this used to consult a
    local SOURCE_LABELS_AR dict, a second copy of the map in
    app/services/source_reference.py. New source_types kept landing in
    one and not the other, and the fallback here was `(st, None)` — the
    RAW English key as the label, so `capital_injection` was printed to
    the user. There is now one map; this reads it.

    The one thing the local map did that resolve_reference can't: link a
    manual entry (source_type IS NULL) to its own journal entry, which
    needs entry.id rather than source_id. That stays here, in the caller.
    """
    ref = resolve_reference(entry.source_type, entry.source_id)
    link = ref["url"]
    if not link and not entry.source_type:
        try:
            link = url_for("journals.view", entry_id=entry.id)
        except Exception:
            link = None
    return ref["label"], link


@bp.route("/<int:account_id>/ledger")
@login_required
def ledger(account_id):
    """MARSOUD-55 — detailed running-balance view of one account's journal
    lines. Excludes paused entries so the running total matches the
    balance shown in the account tree."""
    acc = db.session.get(Account, account_id)
    if not acc or acc.company_id != g.active_company.id:
        abort(404)

    # Date filters (optional, nullable in any direction).
    df_raw = (request.args.get("from") or "").strip()
    dt_raw = (request.args.get("to") or "").strip()
    df = None
    dt = None
    try:
        df = datetime.strptime(df_raw, "%Y-%m-%d").date() if df_raw else None
    except ValueError:
        df = None
    try:
        dt = datetime.strptime(dt_raw, "%Y-%m-%d").date() if dt_raw else None
    except ValueError:
        dt = None

    q = (db.session.query(JournalLine, JournalEntry)
         .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
         .filter(JournalLine.account_id == acc.id,
                 JournalEntry.is_active.is_(True))
         .order_by(JournalEntry.date.asc(), JournalEntry.id.asc(),
                   JournalLine.id.asc()))
    if df:
        q = q.filter(JournalEntry.date >= df)
    if dt:
        q = q.filter(JournalEntry.date <= dt)
    rows = q.all()

    # Build the rendered list with running balance respecting normal_side.
    debit_normal = acc.normal_side == NormalSide.DEBIT
    running = 0.0
    rendered = []
    for line, entry in rows:
        debit = float(line.debit_base or line.debit or 0)
        credit = float(line.credit_base or line.credit or 0)
        delta = (debit - credit) if debit_normal else (credit - debit)
        running += delta
        src_label, src_link = _resolve_source(entry)
        rendered.append({
            "line_id": line.id,
            "entry": entry,
            "date": entry.date,
            "memo": line.memo or entry.description,
            "src_label": src_label,
            "src_link": src_link,
            "debit": debit,
            "credit": credit,
            "running": running,
        })

    totals = {
        "debit": sum(r["debit"] for r in rendered),
        "credit": sum(r["credit"] for r in rendered),
        "balance": running,
    }

    return render_template(
        "accounts/ledger.html",
        account=acc, rows=rendered, totals=totals,
        date_from=df_raw, date_to=dt_raw,
    )
