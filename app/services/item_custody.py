"""MARSOUD-ITEM-CUSTODY-01 (2026-08-07) — item-custody service.

The mutation funnel for physical-item custody. Every state change
lives here — direct model writes bypass invariants and will
corrupt state.

FIVE OUTCOMES drive different accounting effects:

  RETURNED_GOOD        → status flip; no journal
  RETURNED_DAMAGED
    on standalone item, charged=True   → Dr 2130-emp / Cr 5930
    on standalone item, charged=False  → note only, no journal
    on fixed-asset-linked              → disposal_pending_at + bell
                                          → complete_disposal_for_custody
                                            calls dispose_asset()
  LOST                 → same rules as RETURNED_DAMAGED
  TRANSFERRED          → atomic close-old + open-new; no journal;
                          transferred_to_custody_id chain link

The bridge to asset-disposal (`complete_disposal_for_custody`)
uses the `charged_account_id` seam the disposal ticket exposed —
this ticket never posts a disposal journal directly; it delegates
to `dispose_asset()` with the right account resolved.
"""
from datetime import date, datetime

from app import db
from app.models import (
    Employee, Department, EmployeeStatus, FixedAsset, Account,
    CustodyItem, ItemCustodyRequest, ItemCustody,
    CustodyHolderType, ItemCustodyRequestStatus, ItemCustodyStatus,
)
from app.services.ledger import (
    post_journal, get_account_by_code, LedgerError,
)


class ItemCustodyError(Exception):
    """User-facing validation error on the item-custody flow."""


# ═══════════════════════════════════════════════════════════════
# Holder resolution (shared shape with cash_custody).
# ═══════════════════════════════════════════════════════════════
def _resolve_holder(company_id, holder_type, holder_id):
    if not isinstance(holder_type, CustodyHolderType):
        try:
            holder_type = CustodyHolderType(str(holder_type).upper())
        except ValueError:
            raise ItemCustodyError("نوع الحامل غير صالح")

    if holder_type == CustodyHolderType.EMPLOYEE:
        emp = db.session.get(Employee, int(holder_id))
        if not emp or emp.company_id != company_id:
            raise ItemCustodyError("الموظف غير موجود")
        # Terminated employees can't take new custody; they can
        # still settle an existing one they already hold.
        emp_status = str(getattr(emp.status, "value", emp.status))
        if emp_status == "TERMINATED":
            raise ItemCustodyError(
                "لا يمكن تسليم عهدة لموظف موقوف / مُنهى خدمته")
        return emp, holder_type
    else:
        dept = db.session.get(Department, int(holder_id))
        if not dept or dept.company_id != company_id:
            raise ItemCustodyError("القسم غير موجود")
        if not dept.is_active:
            raise ItemCustodyError("القسم غير نشط")
        return dept, holder_type


# ═══════════════════════════════════════════════════════════════
# Queries
# ═══════════════════════════════════════════════════════════════
def active_custody_for_item(item_id):
    return ItemCustody.query.filter_by(
        item_id=item_id, status=ItemCustodyStatus.ACTIVE).first()


def custodies_for_holder(holder_type, holder_id, active_only=True):
    q = ItemCustody.query
    if holder_type == CustodyHolderType.EMPLOYEE:
        q = q.filter_by(employee_id=holder_id)
    else:
        q = q.filter_by(department_id=holder_id)
    if active_only:
        q = q.filter_by(status=ItemCustodyStatus.ACTIVE)
    return q.order_by(ItemCustody.created_at.desc()).all()


def items_available_for_company(company_id):
    """Items that could be requested: active + no ACTIVE custody."""
    # Sub-query for item_ids that DO have an active custody.
    active_ids = db.session.query(ItemCustody.item_id).filter(
        ItemCustody.company_id == company_id,
        ItemCustody.status == ItemCustodyStatus.ACTIVE,
    ).subquery()
    return CustodyItem.query.filter(
        CustodyItem.company_id == company_id,
        CustodyItem.is_active.is_(True),
        ~CustodyItem.id.in_(active_ids),
    ).order_by(CustodyItem.name).all()


def items_pending_disposal(company_id):
    """LOST / DAMAGED custodies for fixed-asset-linked items that
    haven't been disposed yet."""
    return ItemCustody.query.filter(
        ItemCustody.company_id == company_id,
        ItemCustody.disposal_pending_at.isnot(None),
        ItemCustody.disposal_asset_result_id.is_(None),
    ).order_by(ItemCustody.disposal_pending_at.asc()).all()


def pending_requests_for_company(company_id):
    return ItemCustodyRequest.query.filter_by(
        company_id=company_id,
        status=ItemCustodyRequestStatus.PENDING,
    ).order_by(ItemCustodyRequest.created_at.desc()).all()


# ═══════════════════════════════════════════════════════════════
# Item CRUD
# ═══════════════════════════════════════════════════════════════
def create_item(company_id, *, name, serial_number=None,
                 category=None, fixed_asset_id=None,
                 estimated_value=None, created_by=None):
    """Register a new item. Exactly one of fixed_asset_id or
    estimated_value must be meaningful; refuses double-typing.

    If fixed_asset_id is set, the asset must exist, belong to the
    company, and NOT be disposed (a disposed asset has no
    ownership to track)."""
    if not (name or "").strip():
        raise ItemCustodyError("اسم العنصر مطلوب")

    if fixed_asset_id and estimated_value:
        raise ItemCustodyError(
            "اختر واحد فقط: أصل ثابت موجود أو تقييم يدوي — لا الاثنين معاً")

    if fixed_asset_id:
        asset = db.session.get(FixedAsset, int(fixed_asset_id))
        if not asset or asset.company_id != company_id:
            raise ItemCustodyError("الأصل الثابت غير موجود")
        if asset.is_disposed:
            raise ItemCustodyError(
                "لا يمكن ربط عنصر بأصل مشطوب — الأصل خرج من الدفاتر")

    ev = None
    if estimated_value not in (None, ""):
        try:
            ev = round(float(estimated_value), 2)
        except (TypeError, ValueError):
            raise ItemCustodyError("قيمة التقييم غير صالحة")
        if ev < 0:
            raise ItemCustodyError("قيمة التقييم لا يمكن أن تكون سالبة")

    item = CustodyItem(
        company_id=company_id,
        name=name.strip(),
        serial_number=(serial_number or "").strip() or None,
        category=(category or "").strip() or None,
        fixed_asset_id=int(fixed_asset_id) if fixed_asset_id else None,
        estimated_value=ev,
        is_active=True,
        created_by=created_by,
    )
    db.session.add(item)
    db.session.commit()
    return item


# ═══════════════════════════════════════════════════════════════
# Request lifecycle
# ═══════════════════════════════════════════════════════════════
def request_item_custody(company_id, item_id, holder_type, holder_id,
                          *, purpose, created_by=None):
    """Submit a request. Refuses if the item is already in ACTIVE
    custody OR if a pending request already exists for this
    holder+item pair."""
    if not (purpose or "").strip():
        raise ItemCustodyError("السبب مطلوب")

    item = db.session.get(CustodyItem, int(item_id))
    if not item or item.company_id != company_id:
        raise ItemCustodyError("العنصر غير موجود")
    if not item.is_active:
        raise ItemCustodyError("العنصر غير نشط — لا يمكن طلبه")

    holder, holder_type = _resolve_holder(
        company_id, holder_type, holder_id)

    if active_custody_for_item(item.id):
        raise ItemCustodyError(
            f"العنصر «{item.name}» عليه عهدة نشطة بالفعل — "
            "لازم تُسوَّى قبل طلب جديد")

    # Refuse duplicate pending request for the same holder+item.
    dup_q = ItemCustodyRequest.query.filter_by(
        company_id=company_id, item_id=item.id,
        status=ItemCustodyRequestStatus.PENDING)
    if holder_type == CustodyHolderType.EMPLOYEE:
        dup_q = dup_q.filter_by(employee_id=holder.id)
    else:
        dup_q = dup_q.filter_by(department_id=holder.id)
    if dup_q.first():
        raise ItemCustodyError("يوجد طلب سابق قيد المراجعة لنفس العنصر لك")

    req = ItemCustodyRequest(
        company_id=company_id,
        item_id=item.id,
        holder_type=holder_type,
        employee_id=holder.id if holder_type == CustodyHolderType.EMPLOYEE else None,
        department_id=holder.id if holder_type == CustodyHolderType.DEPARTMENT else None,
        purpose=purpose.strip(),
        status=ItemCustodyRequestStatus.PENDING,
        created_by=created_by,
    )
    db.session.add(req)
    db.session.commit()
    _notify_approvers(req)
    return req


def approve_item_request(req, *, reviewer_id, handed_over_on=None,
                          condition_at_handover=None,
                          review_note=None):
    """Approve → hand over. The active-custody guard is re-checked
    HERE at approval time to catch the race where two managers
    approve two different requests for the same item simultaneously."""
    if req.status != ItemCustodyRequestStatus.PENDING:
        raise ItemCustodyError("يمكن اعتماد الطلبات في حالة الانتظار فقط")

    # Race guard — check again NOW that another approval didn't
    # slip in between the request being viewed and this call.
    if active_custody_for_item(req.item_id):
        raise ItemCustodyError(
            f"لا يمكن اعتماد الطلب — العنصر «{req.item.name}» "
            "بقى عليه عهدة نشطة")

    custody = hand_over_item(
        req.company_id, req.item_id,
        req.holder_type,
        (req.employee_id if req.holder_type == CustodyHolderType.EMPLOYEE
         else req.department_id),
        handed_over_on=handed_over_on,
        condition_at_handover=condition_at_handover,
        request=req, actor_id=reviewer_id,
    )

    req.status = ItemCustodyRequestStatus.APPROVED
    req.reviewed_by = reviewer_id
    req.reviewed_at = datetime.utcnow()
    if review_note:
        req.review_note = review_note
    db.session.commit()
    return custody


def reject_item_request(req, *, reviewer_id, review_note=None):
    if req.status != ItemCustodyRequestStatus.PENDING:
        raise ItemCustodyError("يمكن رفض الطلبات في حالة الانتظار فقط")
    req.status = ItemCustodyRequestStatus.REJECTED
    req.reviewed_by = reviewer_id
    req.reviewed_at = datetime.utcnow()
    if review_note:
        req.review_note = review_note
    db.session.commit()

    # Notify the employee-holder (department requests have no user).
    if req.holder_type == CustodyHolderType.EMPLOYEE and req.employee:
        user_id = req.employee.user_id
        if user_id:
            _notify(user_id, req.company_id,
                     title=f"تم رفض طلب عهدة العنصر «{req.item.name}»",
                     body=(review_note or "").strip() or None)
    return req


# ═══════════════════════════════════════════════════════════════
# Handover — creates an ACTIVE custody row
# ═══════════════════════════════════════════════════════════════
def hand_over_item(company_id, item_id, holder_type, holder_id,
                    *, handed_over_on=None,
                    condition_at_handover=None,
                    request=None, actor_id=None):
    """Create the ACTIVE custody row. NO JOURNAL — ownership hasn't
    moved; this is administrative tracking. Even for a fixed-asset-
    linked item, the asset stays on the books unchanged."""
    item = db.session.get(CustodyItem, int(item_id))
    if not item or item.company_id != company_id:
        raise ItemCustodyError("العنصر غير موجود")
    if not item.is_active:
        raise ItemCustodyError("العنصر غير نشط")

    holder, holder_type = _resolve_holder(
        company_id, holder_type, holder_id)

    if active_custody_for_item(item.id):
        raise ItemCustodyError(
            f"العنصر «{item.name}» عليه عهدة نشطة بالفعل")

    custody = ItemCustody(
        company_id=company_id,
        item_id=item.id,
        request_id=request.id if request else None,
        holder_type=holder_type,
        employee_id=holder.id if holder_type == CustodyHolderType.EMPLOYEE else None,
        department_id=holder.id if holder_type == CustodyHolderType.DEPARTMENT else None,
        handed_over_on=handed_over_on or date.today(),
        condition_at_handover=(condition_at_handover or "").strip() or None,
        status=ItemCustodyStatus.ACTIVE,
        created_by=actor_id,
    )
    db.session.add(custody)
    db.session.commit()

    _notify_holder(
        custody,
        title=f"📦 تم تسليمك عهدة عينية: {item.name}",
        body=(condition_at_handover or "").strip() or None)
    return custody


# ═══════════════════════════════════════════════════════════════
# Settlement — the accounting/lifecycle layer
# ═══════════════════════════════════════════════════════════════
def settle_item_custody(custody, outcome, *, settled_on=None,
                         condition_at_return=None,
                         settlement_note=None,
                         damage_value=0,
                         charged_to_employee=False,
                         transfer_holder_type=None,
                         transfer_holder_id=None,
                         actor_id=None):
    """Settle an ACTIVE custody with one of the five outcomes.

    See module docstring for the accounting matrix. `transfer_*`
    args are only used when outcome=TRANSFERRED (target holder for
    the new custody row)."""
    if custody.status != ItemCustodyStatus.ACTIVE:
        raise ItemCustodyError(
            "يمكن تسوية العهد النشطة فقط "
            f"(الحالة الحالية: {custody.status.value})")

    if not isinstance(outcome, ItemCustodyStatus):
        try:
            outcome = ItemCustodyStatus(str(outcome).upper())
        except ValueError:
            raise ItemCustodyError(f"نتيجة التسوية غير صالحة: {outcome!r}")
    if outcome == ItemCustodyStatus.ACTIVE:
        raise ItemCustodyError("لا يمكن تسوية العهدة بحالة ACTIVE")

    settled_on = settled_on or date.today()

    # ─── TRANSFERRED — atomic close + open ────────────────────
    if outcome == ItemCustodyStatus.TRANSFERRED:
        if transfer_holder_type is None or transfer_holder_id is None:
            raise ItemCustodyError(
                "لتحويل العهدة، حدد الحامل الجديد")
        # Close the old row first — sets settled_on so the ACTIVE
        # invariant is momentarily zero-active before the new row
        # opens. Both happen in the same commit.
        custody.status = ItemCustodyStatus.TRANSFERRED
        custody.settled_on = settled_on
        custody.settled_by = actor_id
        custody.settlement_note = (settlement_note or "").strip() or None
        custody.condition_at_return = (
            condition_at_return or "").strip() or None
        custody.overdue_notified_at = None
        db.session.flush()   # commits happens after new-row insert

        # Open the new active custody. Same funnel guards run.
        # NOTE: hand_over_item calls commit at the end, which
        # closes the transaction with both rows present.
        new_custody = hand_over_item(
            custody.company_id, custody.item_id,
            transfer_holder_type, transfer_holder_id,
            handed_over_on=settled_on,
            condition_at_handover=(condition_at_return or "").strip()
                                    or "منقولة من عهدة سابقة",
            actor_id=actor_id,
        )
        custody.transferred_to_custody_id = new_custody.id
        db.session.commit()
        _log(custody, "TRANSFER",
              f"تحويل عهدة {custody.item.name} → عهدة #{new_custody.id}")
        return custody

    # ─── RETURNED_GOOD — pure status flip ────────────────────
    if outcome == ItemCustodyStatus.RETURNED_GOOD:
        custody.status = ItemCustodyStatus.RETURNED_GOOD
        custody.settled_on = settled_on
        custody.settled_by = actor_id
        custody.settlement_note = (settlement_note or "").strip() or None
        custody.condition_at_return = (
            condition_at_return or "").strip() or None
        custody.overdue_notified_at = None
        db.session.commit()
        _log(custody, "RETURN_GOOD",
              f"استرجاع عنصر «{custody.item.name}» بحالة سليمة")
        return custody

    # ─── RETURNED_DAMAGED / LOST ──────────────────────────────
    # Two flavours: standalone (may post a journal) vs fixed-asset-
    # linked (defers to disposal).
    if outcome not in (ItemCustodyStatus.RETURNED_DAMAGED,
                        ItemCustodyStatus.LOST):
        raise ItemCustodyError(f"نتيجة غير مدعومة: {outcome.value}")

    # Common column updates for both flavours.
    custody.status = outcome
    custody.settled_on = settled_on
    custody.settled_by = actor_id
    custody.settlement_note = (settlement_note or "").strip() or None
    custody.condition_at_return = (
        condition_at_return or "").strip() or None
    custody.overdue_notified_at = None
    try:
        damage_value = round(float(damage_value or 0), 2)
    except (TypeError, ValueError):
        raise ItemCustodyError("قيمة الضرر غير صالحة")
    if damage_value < 0:
        raise ItemCustodyError("قيمة الضرر لا يمكن أن تكون سالبة")
    custody.damage_value = damage_value
    custody.charged_to_employee = bool(charged_to_employee)

    # charged_to_employee only makes sense with employee-held custody.
    if custody.charged_to_employee and custody.holder_type != CustodyHolderType.EMPLOYEE:
        raise ItemCustodyError(
            "تحميل القيمة على الموظف متاح فقط للعهد الفردية")

    item = custody.item
    is_fixed_asset = bool(item.fixed_asset_id)

    if is_fixed_asset:
        # Defer to accountant — set the disposal-pending flag +
        # fire a bell. The dispose_asset() call happens later via
        # complete_disposal_for_custody with full accountant context.
        custody.disposal_pending_at = datetime.utcnow()
        db.session.commit()
        _notify_approvers_for_disposal(custody)
        _log(custody, "AWAIT_DISPOSAL",
              f"العنصر «{item.name}» يحتاج قرار شطب")
        return custody

    # Standalone path.
    if custody.charged_to_employee:
        if damage_value <= 0.005:
            raise ItemCustodyError(
                "لازم قيمة ضرر أكبر من صفر عشان تحمّلها على الموظف")
        # Dr employee 2130 leaf / Cr 5930 (Other Misc Expenses).
        # The Cr side re-classifies the original expense — the item
        # was already expensed at purchase; charging the employee
        # partially recovers that. 5930 is a general-purpose
        # miscellaneous account that already exists in the seed.
        from app.services.subsidiary import party_payroll_account
        emp_leaf = party_payroll_account(custody.employee)
        misc_exp = get_account_by_code(custody.company_id, "5930")
        if not misc_exp:
            raise ItemCustodyError(
                "حساب المصروفات المتنوعة (5930) غير موجود")
        entry = post_journal(
            company_id=custody.company_id,
            description=(f"مطالبة موظف بقيمة عهدة عينية تالفة/فاقدة: "
                          f"{item.name} — {damage_value:.2f}"),
            lines=[
                {"account_id": emp_leaf.id, "debit": damage_value,
                 "credit": 0,
                 "memo": f"تحميل قيمة {item.name}"},
                {"account_id": misc_exp.id, "debit": 0,
                 "credit": damage_value,
                 "memo": f"استرداد قيمة {item.name} من الموظف"},
            ],
            entry_date=settled_on,
            reference=f"IC-CHG-{custody.id}",
            created_by=actor_id,
            source_type="item_custody_charge",
            source_id=custody.id,
        )
        custody.journal_entry_id = entry.id

    # Standalone item is fully consumed — retire it so it doesn't
    # appear in the "available" list again.
    item.is_active = False
    db.session.commit()

    _log(custody,
          "CHARGE" if custody.charged_to_employee else "LOSS_NOTE",
          f"تسوية عهدة {item.name}: {outcome.value}"
          + (f" — تحميل {damage_value:.2f} على الموظف"
             if custody.charged_to_employee else ""))
    return custody


# ═══════════════════════════════════════════════════════════════
# The bridge to asset-disposal
# ═══════════════════════════════════════════════════════════════
def complete_disposal_for_custody(custody, *, disposal_date, reason,
                                    proceeds=0, note=None,
                                    funding="cash", actor_id=None):
    """Bridge into `dispose_asset()` for a fixed-asset-linked
    custody that's awaiting disposal.

    Resolves `charged_account_id` to the employee's 2130 leaf when
    `custody.charged_to_employee` — that's the exact seam the
    asset-disposal ticket exposed. Otherwise passes None and the
    loss lands on 5950 (default).

    After successful disposal:
      · custody.disposal_pending_at = NULL
      · custody.disposal_asset_result_id = asset.id (traceable)
      · custody.item.is_active = False (asset gone → item retired)"""
    if custody.status not in (ItemCustodyStatus.LOST,
                               ItemCustodyStatus.RETURNED_DAMAGED):
        raise ItemCustodyError(
            "الشطب متاح فقط لعهد بحالة LOST / RETURNED_DAMAGED")
    if custody.disposal_pending_at is None:
        raise ItemCustodyError("العهدة ليست بانتظار قرار شطب")
    if custody.disposal_asset_result_id is not None:
        raise ItemCustodyError("العهدة تم شطب أصلها بالفعل")
    if not custody.item or not custody.item.fixed_asset_id:
        raise ItemCustodyError(
            "العنصر مش مربوط بأصل ثابت — لا حاجة للشطب")

    # Resolve the seam.
    charged_account_id = None
    if custody.charged_to_employee:
        if custody.holder_type != CustodyHolderType.EMPLOYEE:
            raise ItemCustodyError(
                "لا يمكن تحميل القيمة على قسم — عهدة الأصل موقعة بموظف")
        from app.services.subsidiary import party_payroll_account
        emp_leaf = party_payroll_account(custody.employee)
        charged_account_id = emp_leaf.id

    from app.services.assets import dispose_asset
    asset = dispose_asset(
        custody.item.fixed_asset_id,
        disposal_date=disposal_date,
        reason=reason,
        proceeds=proceeds,
        charged_account_id=charged_account_id,
        note=note or f"شطب أصل بسبب فقد/تلف مع الموظف — عهدة #{custody.id}",
        funding=funding,
        created_by=actor_id,
    )
    custody.disposal_pending_at = None
    custody.disposal_asset_result_id = asset.id
    custody.item.is_active = False
    db.session.commit()

    _log(custody, "DISPOSED",
          f"تم شطب أصل «{custody.item.name}» عبر عهدة #{custody.id}")
    return custody


# ═══════════════════════════════════════════════════════════════
# Cron sweep — one-shot bell for long-active custodies
# ═══════════════════════════════════════════════════════════════
def sweep_long_active_custodies(company_id, *, threshold_days=90):
    """Fire ONE bell notification per custody that's been ACTIVE
    longer than `threshold_days`, deduped via overdue_notified_at.

    Cleared on any settlement so a re-issued custody can re-notify.
    Mirrors cash-custody's sweep pattern."""
    from datetime import timedelta
    cutoff = date.today() - timedelta(days=threshold_days)
    overdue = ItemCustody.query.filter(
        ItemCustody.company_id == company_id,
        ItemCustody.status == ItemCustodyStatus.ACTIVE,
        ItemCustody.handed_over_on < cutoff,
        ItemCustody.overdue_notified_at.is_(None),
    ).all()
    if not overdue:
        return 0

    from app.models.user import user_companies
    from app.services.opsflow_extras import notify as _notify_bell
    from app.models import NotificationKind
    from flask import url_for
    rows = db.session.execute(
        user_companies.select().where(
            (user_companies.c.company_id == company_id) &
            (user_companies.c.role.in_(
                ["owner", "admin", "accountant"]))
        )
    ).fetchall()
    recipient_ids = {r.user_id for r in rows}

    for cust in overdue:
        days_held = (date.today() - cust.handed_over_on).days
        try:
            link = url_for("item_custody.detail",
                           custody_id=cust.id)
        except Exception:
            link = None
        for uid in recipient_ids:
            try:
                _notify_bell(
                    uid, company_id=company_id,
                    kind=NotificationKind.TASK_ASSIGNED,
                    title=(f"📦 عهدة عينية نشطة منذ {days_held} يوم: "
                            f"{cust.item.name}"),
                    body=(f"الحامل: {cust.holder_name} — يُفضَّل "
                           f"مراجعة الحالة"),
                    link_url=link)
            except Exception:
                from flask import current_app
                current_app.logger.exception(
                    "item-custody long-active notify failed")
        cust.overdue_notified_at = datetime.utcnow()
    db.session.commit()
    return len(overdue) * max(len(recipient_ids), 1)


# ═══════════════════════════════════════════════════════════════
# Notifications
# ═══════════════════════════════════════════════════════════════
def _notify(user_id, company_id, *, title, body=None, link_url=None):
    try:
        from app.services.opsflow_extras import notify
        from app.models import NotificationKind
        notify(user_id, company_id=company_id,
               kind=NotificationKind.TASK_ASSIGNED,
               title=title, body=body, link_url=link_url)
    except Exception:
        from flask import current_app
        current_app.logger.exception("item-custody notify failed")


def _notify_holder(custody, *, title, body=None):
    if custody.holder_type != CustodyHolderType.EMPLOYEE:
        return
    emp = custody.employee
    if not emp or not emp.user_id:
        return
    from flask import url_for
    try:
        link = url_for("portal_emp.items_list")
    except Exception:
        link = None
    _notify(emp.user_id, custody.company_id,
             title=title, body=body, link_url=link)


def _notify_approvers(req):
    """Ping custody.manage holders on a new item request."""
    try:
        from flask import url_for
        from app.models.user import user_companies
        rows = db.session.execute(
            user_companies.select().where(
                (user_companies.c.company_id == req.company_id) &
                (user_companies.c.role.in_(
                    ["owner", "admin", "accountant"]))
            )
        ).fetchall()
        try:
            link = url_for("item_custody.requests")
        except Exception:
            link = None
        for r in rows:
            _notify(r.user_id, req.company_id,
                     title=(f"📦 طلب عهدة عينية جديد: "
                             f"{req.item.name}"),
                     body=f"من {req.holder_name}",
                     link_url=link)
    except Exception:
        from flask import current_app
        current_app.logger.exception(
            "item-custody request notify failed")


def _notify_approvers_for_disposal(custody):
    """Ping custody.manage holders when a fixed-asset-linked
    custody hits LOST/DAMAGED and needs the disposal decision."""
    try:
        from flask import url_for
        from app.models.user import user_companies
        rows = db.session.execute(
            user_companies.select().where(
                (custody_id_column := user_companies.c.company_id) == custody.company_id
            ).where(
                user_companies.c.role.in_(
                    ["owner", "admin", "accountant"])
            )
        ).fetchall()
        try:
            link = url_for("item_custody.detail",
                           custody_id=custody.id)
        except Exception:
            link = None
        for r in rows:
            _notify(r.user_id, custody.company_id,
                     title=(f"⚠️ قرار شطب مطلوب: "
                             f"«{custody.item.name}»"),
                     body=(f"العنصر مربوط بأصل ثابت وحالته الآن "
                            f"{custody.status.value} — راجع قرار "
                            f"الشطب"),
                     link_url=link)
    except Exception:
        from flask import current_app
        current_app.logger.exception(
            "item-custody disposal-pending notify failed")


def _log(custody, action_type, label):
    try:
        from app.services.activity import log_action
        log_action(action_type=action_type,
                    entity_type="item_custody",
                    entity_id=custody.id, entity_label=label,
                    company_id=custody.company_id)
    except Exception:
        pass
