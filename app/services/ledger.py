"""Double-entry posting service. Every accounting event flows through here."""
from datetime import date, datetime
from app import db
from app.models import JournalEntry, JournalLine, Account
from app.services.numbering import next_number


class LedgerError(Exception):
    pass


def post_journal(
    company_id,
    description,
    lines,
    entry_date=None,
    reference=None,
    currency=None,
    exchange_rate=1.0,
    created_by=None,
    source_type=None,
    source_id=None,
    cashflow_category=None,
):
    """Post a balanced journal entry.

    lines: list of dicts: {account_id, debit, credit, memo?}

    currency: omit to inherit the company's base_currency. This used to
    default to a hardcoded "SAR", and roughly half the callers omit it —
    so an EGP company had most of its journals stamped SAR. Passing an
    explicit currency still wins (multi-currency invoices rely on that).
    """
    if not lines or len(lines) < 2:
        raise LedgerError("القيد يجب أن يحتوي على سطرين على الأقل")

    if not currency:
        from app.models import Company
        _co = db.session.get(Company, company_id)
        currency = (_co.base_currency if _co else None) or "SAR"

    if float(exchange_rate or 0) <= 0:
        raise LedgerError("سعر الصرف يجب أن يكون أكبر من صفر")

    total_debit = sum(float(l.get("debit") or 0) for l in lines)
    total_credit = sum(float(l.get("credit") or 0) for l in lines)

    if abs(total_debit - total_credit) > 0.0001:
        raise LedgerError(
            f"القيد غير متوازن: مدين {total_debit:.2f} ≠ دائن {total_credit:.2f}"
        )

    if total_debit <= 0:
        raise LedgerError("القيد لا يمكن أن يكون بقيمة صفر")

    entry = JournalEntry(
        company_id=company_id,
        number=next_number(company_id, "JOURNAL"),
        date=entry_date or date.today(),
        description=description,
        reference=reference,
        currency=currency,
        exchange_rate=exchange_rate,
        created_by=created_by,
        source_type=source_type,
        source_id=source_id,
        cashflow_category=cashflow_category,
    )
    db.session.add(entry)
    db.session.flush()

    for line in lines:
        acc = db.session.get(Account, line["account_id"])
        if not acc or acc.company_id != company_id:
            raise LedgerError(f"الحساب غير موجود أو لا ينتمي للشركة")
        # MARSOUD-COA-REBUILD — header accounts (is_postable=False) are
        # for grouping/reporting only. Refuse any line that lands on one
        # so we fail loud instead of corrupting reports silently.
        if not getattr(acc, "is_postable", True):
            raise LedgerError(
                f"الحساب {acc.code} ({acc.name_ar or acc.name}) "
                f"حساب رئيسي ولا يُسمح بالترحيل عليه مباشرة"
            )
        debit = float(line.get("debit") or 0)
        credit = float(line.get("credit") or 0)
        if debit > 0 and credit > 0:
            raise LedgerError("لا يمكن أن يكون السطر مدين ودائن في نفس الوقت")
        jl = JournalLine(
            entry_id=entry.id,
            account_id=acc.id,
            debit=debit,
            credit=credit,
            debit_base=debit * float(exchange_rate),
            credit_base=credit * float(exchange_rate),
            memo=line.get("memo"),
        )
        db.session.add(jl)

    db.session.commit()
    try:
        from app.services.superadmin import log_platform_action
        log_platform_action("journal_created", target_company_id=company_id,
                            actor_id=created_by,
                            details=f"#{entry.number} — {description[:60]}")
    except Exception:
        pass
    # MARSOUD-ACTLOG-01 — manual journal entries get logged as CREATE.
    # Auto-generated entries from invoices/bills/payroll already log
    # at their own service call sites (more meaningful entity_type).
    if (source_type or "") in ("", "manual"):
        try:
            from app.services.activity import log_action
            log_action(action_type="CREATE", entity_type="journal",
                       entity_id=entry.id,
                       entity_label=f"قيد يومي #{entry.number}",
                       company_id=company_id,
                       extra_data={"description": description[:120]})
        except Exception:
            pass
    return entry


def reverse_journal(entry_id, created_by=None):
    """Create a reversing entry that nullifies the original.

    Also undoes domain-level side-effects when the original journal was
    posted by a higher-level service (e.g. accrual settlement). Without
    this, reversing the journal made the ledger balance again but the
    source row (EmployeeAccrual.settled_at, etc.) stayed dirty and the
    UI never reflected the reversal — see MARSOUD-28.
    """
    original = db.session.get(JournalEntry, entry_id)
    if not original:
        raise LedgerError("القيد غير موجود")
    if original.is_reversal:
        raise LedgerError("لا يمكن عكس قيد عكسي")

    reversed_lines = [
        {
            "account_id": l.account_id,
            "debit": float(l.credit),
            "credit": float(l.debit),
            "memo": l.memo,
        }
        for l in original.lines
    ]

    entry = JournalEntry(
        company_id=original.company_id,
        number=next_number(original.company_id, "JOURNAL"),
        date=date.today(),
        description=f"عكس قيد {original.number or '#' + str(original.id)}: {original.description}",
        reference=original.reference,
        currency=original.currency,
        exchange_rate=original.exchange_rate,
        is_reversal=True,
        reversal_of=original.id,
        created_by=created_by,
    )
    db.session.add(entry)
    db.session.flush()

    for line in reversed_lines:
        jl = JournalLine(
            entry_id=entry.id,
            account_id=line["account_id"],
            debit=line["debit"],
            credit=line["credit"],
            debit_base=line["debit"] * float(original.exchange_rate),
            credit_base=line["credit"] * float(original.exchange_rate),
            memo=line["memo"],
        )
        db.session.add(jl)

    # ─── Domain side-effects: undo the action the original posted ──────
    # If we ever post more journal types via services (vendor-bill payment,
    # invoice payment, etc.), add their reversal here too.
    _undo_source_side_effects(original, reversal=entry)

    db.session.commit()
    try:
        from app.services.superadmin import log_platform_action
        log_platform_action("journal_reversed", target_company_id=entry.company_id,
                            actor_id=created_by,
                            details=f"#{entry.number} reverses #{original.number}")
    except Exception:
        pass
    try:
        from app.services.activity import log_action
        log_action(action_type="UPDATE", entity_type="journal",
                   entity_id=original.id,
                   entity_label=f"عكس قيد #{original.number}",
                   company_id=entry.company_id,
                   extra_data={"reversal_id": entry.id,
                                "reversal_number": entry.number})
    except Exception:
        pass
    return entry


def _undo_source_side_effects(original, reversal=None):
    """Inspect original.source_type / source_id and roll back the matching
    domain row. `reversal` is the reversing entry, for sources that want
    to record which entry undid them. Add more cases as needed.
    """
    src_type = original.source_type
    src_id = original.source_id
    if not src_type or not src_id:
        return

    if src_type == "accrual_settle":
        # The original journal came from settle_accrual(). Find the row
        # and mark it un-settled so the employee's outstanding balance
        # picks the amount back up.
        from app.models.payroll import EmployeeAccrual
        accrual = db.session.get(EmployeeAccrual, src_id)
        if accrual and accrual.settled_at is not None:
            accrual.settled_at = None
            accrual.settlement_journal_entry_id = None

    elif src_type == "employee_advance":
        # MARSOUD-ADVANCES — the original journal disbursed an advance.
        # Reversing it (whether from advances.cancel_advance or straight
        # from the /journals page) must also stop the payroll deduction,
        # otherwise the ledger says the advance never happened while
        # payroll keeps recovering it.
        from app.models.advances import EmployeeAdvance, AdvanceStatus
        adv = db.session.get(EmployeeAdvance, src_id)
        if adv and adv.status == AdvanceStatus.ACTIVE:
            adv.status = AdvanceStatus.CANCELLED
            adv.remaining = 0
            adv.cancelled_at = datetime.utcnow()
            if reversal is not None:
                adv.reversal_entry_id = reversal.id


def get_account_by_code(company_id, code):
    return Account.query.filter_by(company_id=company_id, code=code).first()
