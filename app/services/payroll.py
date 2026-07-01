"""Payroll processing — generates monthly run + journal entry.

Variable inputs (working days, overtime, absence/late/advance, bonus, amount_paid)
are passed per-employee. They are stored on the PayrollLine and reset implicitly
each month because each run gets a fresh set of lines.

Prorated salary uses a 30-day basis (Gulf standard): basic/30 × billable_days.
billable_days defaults to 30 but auto-adjusts when an employee was hired or
terminated mid-period.

SUSPENDED and TERMINATED employees are excluded (unless terminated mid-period —
they still receive their partial-month pay).
"""
from calendar import monthrange
from datetime import date, datetime
from app import db
from app.models import (
    Employee, EmployeeStatus, PayrollRun, PayrollLine, EmployeeAccrual,
)
from app.services.ledger import post_journal, get_account_by_code, LedgerError
from app.services.numbering import next_number


def billable_days_in_period(employee, year, month, override=None):
    """Default billable days for an employee within (year, month).

    Returns the number of days the employee should be paid for, respecting:
      - start_date — if hired mid-month, only count from start_date onwards
      - termination_date — if terminated mid-month, only count up to that date

    Caller-supplied `override` (working_days from form) wins when set, BUT we
    still cap it at the natural billable maximum so the user can't accidentally
    over-pay (e.g., enter 30 for someone hired on the 27th).
    """
    days_in_month = monthrange(year, month)[1]
    period_start = date(year, month, 1)
    period_end = date(year, month, days_in_month)

    eff_start = period_start
    eff_end = period_end

    if employee.start_date and employee.start_date > period_start:
        if employee.start_date > period_end:
            return 0   # not hired yet during this period
        eff_start = employee.start_date

    if employee.termination_date and employee.termination_date < period_end:
        if employee.termination_date < period_start:
            return 0   # already terminated before period
        eff_end = employee.termination_date

    natural_billable = (eff_end - eff_start).days + 1
    natural_billable = max(0, min(natural_billable, days_in_month))

    if override is not None:
        try:
            ov = int(override)
            return max(0, min(ov, natural_billable))
        except (TypeError, ValueError):
            return natural_billable

    return natural_billable


def auto_absence_late_for(employee, year, month):
    """HR-07 — convert AttendanceException rows into monetary absence + late
    amounts for a payroll line.

    Returns `(absence_amount, late_amount, has_exceptions)` where amounts are
    in the company currency, computed as `days × (basic / 30)`. Falls back to
    (0, 0, False) when no exceptions exist (backward-compat with pre-HR-07
    payroll runs).
    """
    try:
        from app.services.leave import attendance_deductions
    except Exception:
        return 0.0, 0.0, False, 0
    info = attendance_deductions(employee.id, year, month)
    if not info.get("has_exceptions"):
        return 0.0, 0.0, False, 0
    daily_rate = float(employee.basic_salary or 0) / 30.0
    absence_amt = round(info["absence_days"] * daily_rate, 2)
    late_amt = round(info["late_days"] * daily_rate, 2)
    return absence_amt, late_amt, True, int(info["absence_days"])


def run_payroll(company_id, year, month, line_inputs=None, created_by=None, send_emails=True):
    """Execute a payroll run.

    line_inputs: optional dict keyed by employee_id with overrides:
        {emp_id: {working_days, overtime, bonus, absence, late, advance, amount_paid}}
        Missing employees use defaults (auto billable_days, no variable amounts,
        amount_paid = net).

    HR-07: when AttendanceException rows exist for an employee in this period,
    absence + late default to the auto-computed amounts. If the form posts a
    different value we honour it and mark the line `attendance_auto_calculated=False`.
    """
    existing = PayrollRun.query.filter_by(
        company_id=company_id, period_year=year, period_month=month
    ).first()
    if existing:
        raise LedgerError(f"كشف رواتب {month}/{year} موجود بالفعل")

    # Include any employee who was active at any point during the period (so
    # someone terminated mid-month still gets their partial pay).
    period_end = date(year, month, monthrange(year, month)[1])
    employees = Employee.query.filter(
        Employee.company_id == company_id,
        db.or_(
            Employee.status == EmployeeStatus.ACTIVE,
            db.and_(
                Employee.status == EmployeeStatus.TERMINATED,
                Employee.termination_date >= date(year, month, 1),
            ),
        ),
    ).all()
    if not employees:
        raise LedgerError("لا يوجد موظفين نشطين")

    run = PayrollRun(
        company_id=company_id,
        number=next_number(company_id, "PAYROLL"),
        period_year=year,
        period_month=month,
    )
    db.session.add(run)
    db.session.flush()

    line_inputs = line_inputs or {}
    total_gross = 0.0
    total_net = 0.0
    total_paid_cash = 0.0
    total_accrued = 0.0
    lines_created = []
    accruals_to_create = []

    for emp in employees:
        inputs = line_inputs.get(emp.id, {})

        working_days = billable_days_in_period(
            emp, year, month, override=inputs.get("working_days"),
        )
        overtime = float(inputs.get("overtime", 0) or 0)
        overtime_hours = float(inputs.get("overtime_hours", 0) or 0)
        absence_days = int(inputs.get("absence_days", 0) or 0)
        bonus = float(inputs.get("bonus", 0) or 0)

        # HR-07 — auto-fill absence/late from attendance exceptions
        auto_absence, auto_late, has_exceptions, auto_absence_count = auto_absence_late_for(emp, year, month)
        if "absence" in inputs and inputs["absence"] not in (None, ""):
            absence = float(inputs["absence"])
        else:
            absence = auto_absence
        if "late" in inputs and inputs["late"] not in (None, ""):
            late = float(inputs["late"])
        else:
            late = auto_late
        # Auto-calculated iff exceptions exist AND submitted values match auto.
        attendance_auto = (
            has_exceptions
            and abs(absence - auto_absence) < 0.01
            and abs(late - auto_late) < 0.01
        )

        advance = float(inputs.get("advance", 0) or 0)

        basic_full = float(emp.basic_salary or 0)
        prorated_basic = (basic_full / 30.0) * max(0, working_days)
        allowances = float(emp.allowances or 0)
        fixed_deductions = float(emp.deductions or 0)

        # MARSOUD-COMM-01 Phase C — sum + settle the rep's outstanding
        # commission rows (positive earnings + negative carry-forwards)
        # before computing net pay. Adds to gross like a bonus does.
        # The rows themselves are flipped to PAID + linked to this run
        # once db.session.commit() lands at the end of run_payroll.
        try:
            from app.services.sales_commissions import settle_commissions_for_employee
            commissions_net, _commission_rows = settle_commissions_for_employee(
                emp, run, period_year=year, period_month=month,
            )
        except Exception:
            import logging
            logging.getLogger("ledgeros.payroll").exception(
                "settle_commissions_for_employee failed for %s — defaulting to 0",
                emp.name,
            )
            commissions_net = 0.0

        gross = prorated_basic + allowances + overtime + bonus + commissions_net
        total_deductions = fixed_deductions + absence + late + advance
        net = round(gross - total_deductions, 2)

        # amount_paid defaults to net (= full payment) when not set
        if "amount_paid" in inputs and inputs["amount_paid"] not in (None, ""):
            amount_paid = round(float(inputs["amount_paid"]), 2)
            amount_paid = max(0.0, min(amount_paid, net))
        else:
            amount_paid = net
        accrued = round(net - amount_paid, 2)

        line = PayrollLine(
            run_id=run.id,
            employee_id=emp.id,
            working_days=working_days,
            basic=round(prorated_basic, 2),
            allowances=allowances,
            overtime=overtime,
            overtime_hours=overtime_hours,
            bonus=bonus,
            deductions=fixed_deductions,
            absence_deduction=absence,
            late_deduction=late,
            advance_deduction=advance,
            net=net,
            amount_paid=amount_paid,
            attendance_auto_calculated=attendance_auto,
            absences_count=absence_days if absence_days > 0 else auto_absence_count,
            # MARSOUD-COMM-01 Phase C — surface settled commissions
            # for the payslip + reporting.
            commissions=round(commissions_net, 2),
        )
        db.session.add(line)
        db.session.flush()
        lines_created.append((emp, line))

        if accrued > 0.005:
            accruals_to_create.append((emp, line, accrued))

        total_gross += gross
        total_net += net
        total_paid_cash += amount_paid
        total_accrued += accrued

    # MARSOUD-PAYROLL-LEDGER-03 — every employee's full NET salary now
    # credits his own sub-account (the accrual leg). For employees paid
    # cash, a second "settlement" entry debits the same sub-account and
    # credits cash. This way an employee's statement shows BOTH movements
    # even when they're paid in full (net balance 0). Before this fix,
    # paid-in-full employees had no entry on their ledger at all.
    from app.services.subsidiary import party_payroll_account
    salary_expense = get_account_by_code(company_id, "5210")
    cash_acc = get_account_by_code(company_id, "1110")
    if not salary_expense or not cash_acc:
        raise LedgerError("حسابات الرواتب أو النقدية غير موجودة")

    # ─── Entry 1: accrual ───────────────────────────────────────────
    # Dr 5210 total_net (single aggregated expense line)
    # Cr each employee's sub-account by their full NET (not just accrued)
    accrual_lines = [
        {"account_id": salary_expense.id,
         "debit": round(total_net, 2), "credit": 0,
         "memo": f"مصروف رواتب {month}/{year}"},
    ]
    # Track sub-accounts so the settlement entry can reuse them
    emp_subaccounts = {}
    for emp, line in lines_created:
        if float(line.net or 0) < 0.005:
            continue   # skip zero-net employees (e.g. all-deducted)
        emp_acct = party_payroll_account(emp)
        emp_subaccounts[emp.id] = emp_acct
        accrual_lines.append({
            "account_id": emp_acct.id, "debit": 0,
            "credit": round(float(line.net), 2),
            "memo": f"استحقاق راتب — {emp.name}",
        })

    entry = post_journal(
        company_id=company_id,
        description=f"رواتب شهر {month}/{year} — استحقاق",
        lines=accrual_lines,
        reference=f"PAYROLL-{year}-{month:02d}",
        created_by=created_by,
        source_type="payroll",
        source_id=run.id,
    )

    # ─── Entry 2: settlement (cash payout, if any) ──────────────────
    # Dr each paid employee's sub-account by amount_paid
    # Cr cash by total_paid_cash
    if total_paid_cash > 0.005:
        settle_lines = []
        for emp, line in lines_created:
            paid = float(line.amount_paid or 0)
            if paid < 0.005:
                continue
            emp_acct = emp_subaccounts.get(emp.id) or party_payroll_account(emp)
            settle_lines.append({
                "account_id": emp_acct.id,
                "debit": round(paid, 2), "credit": 0,
                "memo": f"سداد راتب — {emp.name}",
            })
        settle_lines.append({
            "account_id": cash_acc.id, "debit": 0,
            "credit": round(total_paid_cash, 2),
            "memo": f"صرف نقدي للموظفين — {month}/{year}",
        })
        post_journal(
            company_id=company_id,
            description=f"رواتب شهر {month}/{year} — سداد كاش",
            lines=settle_lines,
            reference=f"PAYROLL-PAY-{year}-{month:02d}",
            created_by=created_by,
            source_type="payroll_settlement",
            source_id=run.id,
        )
    run.total_gross = round(total_gross, 2)
    run.total_net = round(total_net, 2)
    run.journal_entry_id = entry.id

    # Persist per-employee accruals
    for emp, line, amount in accruals_to_create:
        db.session.add(EmployeeAccrual(
            company_id=company_id,
            employee_id=emp.id,
            source_run_id=run.id,
            source_line_id=line.id,
            amount=amount,
        ))

    db.session.commit()

    # Email payslips — non-blocking
    if send_emails:
        try:
            from app.services.email import send_payslip_email
            from app.services.export import export_payslip_pdf
            for emp, line in lines_created:
                if emp.email:
                    try:
                        pdf = export_payslip_pdf(emp, line, run).getvalue()
                    except Exception:
                        pdf = None
                    send_payslip_email(emp, line, run, pdf_bytes=pdf)
        except Exception:
            import logging
            logging.getLogger("ledgeros.payroll").exception("Failed to send payslips")

    # MARSOUD-ACTLOG-01
    try:
        from app.services.activity import log_action
        log_action(action_type="CREATE", entity_type="payroll_run",
                   entity_id=run.id,
                   entity_label=f"كشف رواتب {month}/{year}",
                   company_id=company_id,
                   extra_data={"total_net": float(run.total_net or 0),
                                "year": year, "month": month})
    except Exception:
        pass
    return run


def terminate_employee(employee, reason, termination_date=None, notes=None):
    old_status = getattr(employee.status, "value", str(employee.status))
    employee.status = EmployeeStatus.TERMINATED
    employee.is_active = False
    employee.termination_date = termination_date or date.today()
    employee.termination_reason = reason
    employee.termination_notes = notes
    db.session.commit()
    try:
        from app.services.activity import log_action
        log_action(action_type="UPDATE", entity_type="employee",
                   entity_id=employee.id,
                   entity_label=f"إنهاء عقد: {employee.name}",
                   company_id=employee.company_id,
                   extra_data={"old": {"status": old_status},
                                "new": {"status": "TERMINATED",
                                        "reason": str(reason),
                                        "termination_date": str(employee.termination_date)}})
    except Exception:
        pass
    return employee


def settle_accrual(accrual, payment_method_account_code="1110",
                    amount=None, created_by=None):
    """Pay part or all of an outstanding accrual to the employee.

    MARSOUD-PARTIAL-SETTLE — if `amount` is None (or omitted) the whole
    remaining balance is paid, matching the old behaviour. If a
    specific `amount` is passed, only that much is paid; the balance
    accumulates in `accrual.paid_amount` and remains owed until fully
    settled.

    Every partial payment produces its own journal entry
    (Dr employee sub-account / Cr cash) with source_type='accrual_settle'
    so the audit trail is complete.

    Raises LedgerError if the accrual is already fully paid or the
    requested amount exceeds what's remaining.
    """
    if accrual.is_settled:
        raise LedgerError("هذا المبلغ تم سداده بالكامل مسبقاً")

    remaining = accrual.remaining
    if remaining <= 0.005:
        raise LedgerError("لا يوجد رصيد متبقٍ لهذا المستحق")

    # Default: pay the whole remainder (backwards-compatible).
    pay_amt = float(remaining if amount is None else amount)
    pay_amt = round(pay_amt, 2)
    if pay_amt <= 0.005:
        raise LedgerError("قيمة السداد يجب أن تكون أكبر من صفر")
    # Allow a tiny FP tolerance so "pay the exact remaining" never fails
    if pay_amt > remaining + 0.005:
        raise LedgerError(
            f"القيمة ({pay_amt:.2f}) أكبر من الرصيد المتبقي ({remaining:.2f})"
        )
    # Clamp to remaining so we never overpay by a fractional cent.
    pay_amt = min(pay_amt, remaining)

    company_id = accrual.company_id
    from app.services.subsidiary import party_payroll_account
    salary_payable = party_payroll_account(accrual.employee)
    cash_acc = get_account_by_code(company_id, payment_method_account_code)
    if not salary_payable or not cash_acc:
        raise LedgerError("حسابات السداد غير موجودة")

    # For partial vs full, tag the journal so it's easy to spot in reports.
    is_partial = pay_amt < remaining - 0.005
    label = "سداد جزئي" if is_partial else "سداد كامل"
    entry = post_journal(
        company_id=company_id,
        description=(f"{label} لمستحق راتب — "
                      f"{accrual.employee.name} ({pay_amt:.2f})"),
        lines=[
            {"account_id": salary_payable.id, "debit": pay_amt, "credit": 0,
             "memo": f"{label} — {accrual.employee.name}"},
            {"account_id": cash_acc.id, "debit": 0, "credit": pay_amt,
             "memo": f"صرف نقدي — {accrual.employee.name}"},
        ],
        reference=f"ACCR-{accrual.id}",
        created_by=created_by,
        source_type="accrual_settle",
        source_id=accrual.id,
    )

    # Bump paid_amount + mark settled only when fully paid.
    from decimal import Decimal
    accrual.paid_amount = Decimal(str(
        round(float(accrual.paid_amount or 0) + pay_amt, 2)
    ))
    if accrual.remaining <= 0.005:
        accrual.settled_at = datetime.utcnow()
        accrual.settlement_journal_entry_id = entry.id  # points to the LAST leg
    db.session.commit()
    return accrual


def update_employee(employee, form, *, changed_by_id=None):
    """Update an existing employee. Locks fields that would invalidate
    historical proration if changed retroactively.

    HR-SS — snapshots tracked fields BEFORE mutation and writes
    EmployeeHistory rows for any that actually changed.
    """
    # ── HR-SS snapshot (read every tracked field before we mutate it) ──
    from app.services.hr_self_service import (
        diff_employee_for_history, apply_history_log,
    )
    from app.models import Department

    def _dept_name(dept_id):
        if not dept_id:
            return None
        d = db.session.get(Department, dept_id)
        return d.name if d else None

    def _status_label(s):
        if not s:
            return None
        if hasattr(s, "label_ar"):
            return s.label_ar
        labels = {"ACTIVE": "نشط",
                  "SUSPENDED": "موقوف",
                  "TERMINATED": "منتهي"}
        return labels.get(s.value if hasattr(s, "value") else str(s),
                          str(s))

    history_snapshot = diff_employee_for_history(
        employee, form,
        dept_lookup=_dept_name,
        status_label_lookup=_status_label,
    )

    has_history = employee.payroll_lines and len(list(employee.payroll_lines)) > 0
    employee.name = (form.get("name") or employee.name).strip()
    employee.email = (form.get("email") or "").strip()
    employee.phone = (form.get("phone") or "").strip()
    employee.job_title = (form.get("job_title") or "").strip()

    # Locked fields when payroll history exists
    if not has_history:
        if form.get("employee_number"):
            employee.employee_number = form.get("employee_number").strip()
        if form.get("start_date"):
            employee.start_date = datetime.strptime(form.get("start_date"), "%Y-%m-%d").date()

    ct_str = form.get("contract_type")
    if ct_str:
        from app.models import ContractType
        try:
            employee.contract_type = ContractType[ct_str]
        except KeyError:
            pass

    status_str = form.get("status")
    if status_str:
        try:
            new_status = EmployeeStatus[status_str]
            employee.status = new_status
            employee.is_active = (new_status == EmployeeStatus.ACTIVE)
        except KeyError:
            pass

    for fld in ("basic_salary", "allowances", "deductions"):
        v = form.get(fld)
        if v is not None and v != "":
            try:
                setattr(employee, fld, float(v))
            except ValueError:
                pass

    # ─── HR-01 / HR-02 fields ──────────────────────────────────────────
    from app.models import Gender
    def _parse_date(s):
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    dept_raw = form.get("department_id")
    if dept_raw is not None:
        employee.department_id = int(dept_raw) if dept_raw else None

    mgr_raw = form.get("manager_id")
    if mgr_raw is not None:
        new_mgr_id = int(mgr_raw) if mgr_raw else None
        # Prevent self-referential manager
        if new_mgr_id == employee.id:
            new_mgr_id = None
        employee.manager_id = new_mgr_id

    if "national_id" in form:
        employee.national_id = (form.get("national_id") or "").strip() or None
    if "nationality" in form:
        employee.nationality = (form.get("nationality") or "").strip() or None
    if "date_of_birth" in form:
        employee.date_of_birth = _parse_date(form.get("date_of_birth"))
    if "contract_end_date" in form:
        new_end = _parse_date(form.get("contract_end_date"))
        if new_end != employee.contract_end_date:
            # Reset alert dedup so the next cron tick re-evaluates
            employee.contract_alert_last_sent = None
        employee.contract_end_date = new_end
    if "notes" in form:
        employee.notes = (form.get("notes") or "").strip() or None

    gender_str = form.get("gender")
    if gender_str is not None:
        if gender_str:
            try:
                employee.gender = Gender[gender_str]
            except KeyError:
                pass
        else:
            employee.gender = None

    db.session.commit()

    # HR-SS: write history rows for any fields that actually flipped.
    try:
        apply_history_log(
            employee, history_snapshot,
            dept_lookup=_dept_name,
            status_label_lookup=_status_label,
            changed_by_id=changed_by_id,
        )
    except Exception:
        from flask import current_app
        current_app.logger.exception("apply_history_log failed")

    return employee
