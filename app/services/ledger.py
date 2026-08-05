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

    elif src_type == "open_item":
        # MARSOUD-OPS-FOUNDATION — the creating journal of a two-sided
        # operation. Reversing it means the obligation never existed.
        from app.models import OpenItem
        from app.services.open_items import cancel_open_item
        item = db.session.get(OpenItem, src_id)
        if item is not None:
            # ...but only if nothing has been PAID against it yet.
            #
            # Measured: accrue 1000, settle 500, then reverse the accrual.
            # The reversal debits 2160 by the full 1000 while the
            # settlement had already debited it by 500, leaving 2160 at
            # +500 — real money that left the bank, now stranded on a
            # payable belonging to an item marked CANCELLED, which no
            # screen will ever offer to clear. Both entries balance, so
            # nothing downstream complains.
            #
            # The honest rule is the accounting one: you cannot un-accrue
            # something you have already partly paid. Undo the payments
            # first. Raised before reverse_journal commits, and the
            # journals route already turns LedgerError into a flash.
            live = [s for s in item.settlements if s.reversed_at is None]
            if live:
                paid = sum(float(s.amount or 0) for s in live)
                raise LedgerError(
                    f"لا يمكن عكس هذا القيد: البند مسدَّد بمبلغ "
                    f"{paid:,.2f} في {len(live)} عملية سداد. "
                    "اعكس قيود السداد أولًا ثم أعد المحاولة.")
            cancel_open_item(
                item, reversal_entry_id=reversal.id if reversal else None)

    elif src_type == "open_item_settle":
        # A settlement leg. Reversing it puts the amount back and reopens
        # the item — a settled item whose journal was reversed must not
        # stay settled.
        from app.models import OpenItemSettlement
        from app.services.open_items import reverse_settlement
        leg = db.session.get(OpenItemSettlement, src_id)
        if leg is not None:
            reverse_settlement(leg)

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


# ─── MARSOUD-FINANCIAL-ACCOUNT-FIELD (2026-08-04) ───────────────────────
CASH_CODE = "1110"
BANKS_HEADER_CODE = "1120"


def cash_accounts(company_id, active_only=False):
    """Every postable account that actually holds money — cash + banks.

    MARSOUD-OPS-FOUNDATION (2026-08-05) — reports used to ask for
    `code IN ("1110", "1120")`. 1120 «البنوك» is a non-postable HEADER, so
    no journal line can ever hit it, and every report built on that literal
    silently saw the cash box alone: the cash-flow statement and the
    dashboard's «السيولة المتاحة» KPI were both missing every bank
    movement.

    `active_only=False` by DEFAULT here, unlike the picker. A report must
    still show what moved through a bank account that has since been
    deactivated — dropping it would rewrite history. The picker wants the
    opposite, so it passes True.
    """
    out = []
    for code in (CASH_CODE, BANKS_HEADER_CODE):
        root = get_account_by_code(company_id, code)
        if root:
            out.extend(_collect_postable(root, active_only=active_only))
    return out


def cash_account_ids(company_id, active_only=False):
    """Just the ids — what report queries filter journal lines on."""
    return [a.id for a in cash_accounts(company_id, active_only=active_only)]


def postable_under(company_id, root_code, active_only=True):
    """Every postable account beneath `root_code`, ordered by code.

    MARSOUD-OPS-FOUNDATION (2026-08-05) — the generic form of what the
    money picker was doing for 1110/1120. Every account picker in the
    operations centre goes through here, so "postable only" is a property
    of ONE function rather than a rule each new picker has to remember.
    That matters: a header account is refused by post_journal, so a picker
    that offers one lets the user fill in a whole form and only then be
    told no.

    Returns [] when the root is missing, so a company with a pruned chart
    of accounts gets an empty picker and a clear message rather than a
    crash.
    """
    root = get_account_by_code(company_id, root_code)
    if not root:
        return []
    return _collect_postable(root, active_only=active_only)


def resolve_account_under(company_id, root_code, account_id,
                          missing_msg="اختر الحساب",
                          invalid_msg="الحساب المختار غير صالح",
                          empty_msg=None):
    """Validate a submitted id against what `postable_under` would offer.

    The same shape as resolve_financial_account, and for the same reason:
    re-checking against the OFFERED SET rather than merely "is this an
    account of mine" is what stops a hand-crafted POST landing money on an
    account the picker never showed.
    """
    allowed = {a.id: a for a in postable_under(company_id, root_code)}
    if not allowed:
        raise LedgerError(
            empty_msg or f"لا يوجد حساب قابل للترحيل تحت {root_code} — "
            "راجع شجرة الحسابات")
    try:
        aid = int(account_id or 0)
    except (TypeError, ValueError):
        aid = 0
    if not aid:
        raise LedgerError(missing_msg)
    acc = allowed.get(aid)
    if acc is None:
        raise LedgerError(invalid_msg)
    return acc, (acc.name_ar or acc.name)


def cash_and_bank_accounts(company_id):
    """The accounts money can actually enter or leave, grouped for a
    <select>: [(group_label_ar, [Account, ...]), ...].

    Used by the 🧮 accounting-operations wizards, which ask "which account
    did the money go into" — not "which payment method", a question that
    has no meaning without a customer or a supplier.

    Two hard rules:
      · POSTABLE ONLY. 1120 (البنوك) is a header; post_journal refuses it
        (see the is_postable guard above), so offering it would let the
        user fill in a whole form and only then be told no.
      · Banks are found by walking parent_id down from 1120, not by a
        `code LIKE '112%'` match, so banks a company added itself appear.

    Empty groups are dropped — a company with no bank accounts sees only
    الصندوق. Header accounts are skipped as options but still traversed,
    because their children are the real accounts.
    """
    groups = []
    cash_root = get_account_by_code(company_id, CASH_CODE)
    if cash_root:
        cash = _collect_postable(cash_root, active_only=True)
        if cash:
            groups.append(("الصندوق", cash))
    banks_root = get_account_by_code(company_id, BANKS_HEADER_CODE)
    if banks_root:
        banks = _collect_postable(banks_root, active_only=True)
        if banks:
            groups.append(("البنوك", banks))
    return groups


def _collect_postable(root, active_only=True):
    """Postable accounts in `root`'s subtree, ordered by code.

    `active_only` is True for pickers (don't offer a disabled account) and
    False for reports (a deactivated bank's past movements are still real).
    """
    found = []
    stack = [root]
    seen = set()
    while stack:
        node = stack.pop()
        if node.id in seen:
            continue
        seen.add(node.id)
        if getattr(node, "is_postable", True) and (
                node.is_active or not active_only):
            found.append(node)
        stack.extend(node.children or [])
    return sorted(found, key=lambda a: (a.code or ""))


def resolve_financial_account(company_id, account_id):
    """Validate a submitted account id against `cash_and_bank_accounts`.

    Returns (Account, label_ar). Raises LedgerError when the id is
    missing, from another tenant, or simply not one of the offered
    options — re-checking against the allowed SET, not merely "is this an
    account", so a hand-crafted POST cannot land the money on an
    arbitrary account such as revenue.
    """
    allowed = {a.id: a for _lbl, accs in cash_and_bank_accounts(company_id)
               for a in accs}
    if not allowed:
        raise LedgerError(
            "لا يوجد حساب نقدية أو بنك قابل للترحيل — راجع شجرة الحسابات")
    try:
        aid = int(account_id or 0)
    except (TypeError, ValueError):
        aid = 0
    if not aid:
        raise LedgerError("اختر الحساب المالي")
    acc = allowed.get(aid)
    if acc is None:
        raise LedgerError("الحساب المالي غير صالح")
    return acc, (acc.name_ar or acc.name)
