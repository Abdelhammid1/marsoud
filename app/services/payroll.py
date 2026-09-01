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


def late_month_breakdown(employee_id, year, month, *, policy=None):
    """MARSOUD-VIOLATION-POLICY (2026-08-05) — ticket 6.

    The month's lateness resolved against a violation policy. Returns a
    dict with:

      charged_days      — the days of pay run_payroll will deduct
      pool_used_minutes — how much of the monthly pool was consumed
      pool_remaining    — pool_total - pool_used (>= 0)

    This is the single source of truth for the late math. Both
    compute_late_deduction (payroll) and /my/attendance (T7 employee
    overview) resolve their numbers here, so a change to the algorithm
    lands once and cannot drift between what the employee is TOLD they
    have left and what payroll actually bills.

    Order of application (fixed by the spec):
      1. approved LatePermissionRequests clear their day's minutes,
      2. daily_free_late_minutes_cap forgives the first N minutes of
         every remaining day (use it or lose it),
      3. monthly_free_late_minutes pool absorbs what is left, drawn
         earliest-day-first,
      4. whatever remains is charged as minutes / 60 / 8.

    NO POLICY BRANCH: charged_days is exactly what pre-batch
    attendance_deductions() would have computed — the sum of
    ex.deduction_days() across the month's active LATE rows, rounded
    identically. Pool figures are None because there is no pool to
    speak of. That is the byte-for-byte regression guarantee.
    """
    from collections import defaultdict
    from calendar import monthrange
    from datetime import date as _date
    from app.services.leave import active_exceptions
    from app.models import AttendanceException, AttendanceExceptionType

    days_in_month = monthrange(year, month)[1]
    start = _date(year, month, 1)
    end = _date(year, month, days_in_month)

    late_rows = active_exceptions().filter(
        AttendanceException.employee_id == employee_id,
        AttendanceException.type == AttendanceExceptionType.LATE,
        AttendanceException.date >= start,
        AttendanceException.date <= end,
    ).all()

    if policy is None:
        return {
            "charged_days": round(
                sum(ex.deduction_days() for ex in late_rows), 2),
            "pool_used_minutes": None,
            "pool_remaining": None,
        }

    # 1. Aggregate the raw lateness per day (a day cannot have two LATE
    #    rows — create_exception refuses a duplicate — but a defaultdict
    #    keeps the code trivial and future-proofs a partial-index fix.)
    day_minutes = defaultdict(float)
    for ex in late_rows:
        day_minutes[ex.date] += float(ex.duration_hours or 0) * 60.0

    # 2. Subtract approved permissions for each day. Permissions clear
    #    lateness before any allowance is charged, so an employee who
    #    had a legitimate emergency does not spend their monthly pool
    #    to cover it.
    from app.services.violation import approved_permissions_for
    for p in approved_permissions_for(employee_id, year, month):
        day_minutes[p.request_date] = max(
            0.0,
            day_minutes[p.request_date] - float(p.hours_count or 0) * 60.0)

    # 3. Per-day cap — the first N minutes of every day are free. This
    #    is a per-day allowance (use it or lose it), not a bucket that
    #    accumulates across days.
    cap = int(policy.daily_free_late_minutes_cap or 0)
    if cap:
        day_minutes = {d: max(0.0, m - cap) for d, m in day_minutes.items()}

    # 4. Draw remainder from the monthly pool, earliest-day-first. A day
    #    that fully consumes what is left of the pool still charges the
    #    remainder — the pool is not "absorb this day or none of it".
    pool_total = int(policy.monthly_free_late_minutes or 0)
    pool = pool_total
    total_billable = 0.0
    for d in sorted(day_minutes):
        m = day_minutes[d]
        if pool >= m:
            pool -= m
        else:
            total_billable += m - pool
            pool = 0

    return {
        "charged_days": round(total_billable / 60.0 / 8.0, 2),
        "pool_used_minutes": pool_total - pool,
        "pool_remaining": pool,
    }


def compute_late_deduction(employee_id, year, month, *, policy=None):
    """Days of pay to deduct for the month's lateness. Thin wrapper
    around late_month_breakdown — kept as the public entry point that
    services/leave.py and run_payroll use, so callers that only care
    about the number do not pull the dict apart at every call site.

    Placed in services/payroll.py rather than services/violation.py
    because payroll is where deductions live and services/leave.py
    already imports payroll; putting it here means the call graph does
    not gain a new hop through violation just to reach a number the
    payroll layer owns.
    """
    return late_month_breakdown(
        employee_id, year, month, policy=policy)["charged_days"]


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
    # MARSOUD-ADVANCES — how much of each line's advance_deduction was
    # actually drawn from a tracked EmployeeAdvance. Drives the ledger
    # correction below; keyed by employee id.
    advance_applied = {}

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

        # MARSOUD-ADVANCE-INSTALMENTS (2026-08-05) — `or 0` used to be
        # here, and it was the whole bug: a missing field became a zero
        # deduction, so the automation only worked when the payroll FORM
        # had filled the box. Any other caller deducted nothing and the
        # advance stayed open forever.
        #
        # None now means "the service works it out from the open
        # balance". An explicit 0 still means a deliberate skip, and a
        # typed number is still respected — silence is the only thing
        # whose meaning changed.
        #
        # The real amount is known only after apply_advance_deduction
        # runs (it is capped by the remaining balance), so the line is
        # created with a provisional figure and corrected below.
        advance_input = inputs.get("advance")
        if advance_input is None or str(advance_input).strip() == "":
            advance_input = None
            from app.services.advances import installment_due_for
            advance = installment_due_for(emp)
        else:
            try:
                advance = round(float(advance_input), 2)
            except (TypeError, ValueError):
                advance = 0.0

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
        # MARSOUD-OPS-HUB-EXPANSION-01 (2026-08-08, Phase 1) — GOSI
        # + income-tax withholding + employer share, computed off
        # basic_salary (the legal base — the same base GOSI uses,
        # unrelated to allowances/overtime). Zero-rate employees hit
        # every path with 0 and land byte-identical to the pre-fix
        # journal (checked by audit).
        emp_insurance = round(basic_full * float(emp.insurance_rate or 0)
                              / 100.0, 2)
        emp_income_tax = round(basic_full * float(emp.income_tax_rate or 0)
                                / 100.0, 2)
        employer_ins = round(basic_full
                              * float(emp.company_insurance_rate or 0)
                              / 100.0, 2)
        total_deductions = (fixed_deductions + absence + late + advance
                             + emp_insurance + emp_income_tax)
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
            # MARSOUD-OPS-HUB-EXPANSION-01 (Phase 1) — snapshot of the
            # withholding amounts on the payslip row itself, so a rate
            # change AFTER the run doesn't retroactively rewrite it.
            insurance_deduction=emp_insurance,
            income_tax_deduction=emp_income_tax,
            employer_insurance_share=employer_ins,
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

        # MARSOUD-ADVANCES — draw the deduction down from the employee's
        # open advance and remember how much actually landed against a
        # tracked balance. A number typed by hand with no advance behind
        # it applies 0 and keeps the old ledger behaviour.
        #
        # MARSOUD-ADVANCE-INSTALMENTS — the run and the period go in so
        # the service can write the instalment row and refuse to recover
        # the same period twice. `advance_input` is passed rather than
        # `advance`: None still means "work it out", which is the whole
        # point of the change.
        try:
            from app.services.advances import apply_advance_deduction
            applied = apply_advance_deduction(
                emp, advance_input, run=run,
                period_year=year, period_month=month, payroll_line=line)
            advance_applied[emp.id] = applied
        except Exception:
            import logging
            logging.getLogger("ledgeros.payroll").exception(
                "apply_advance_deduction failed for %s — treating as untracked",
                emp.name,
            )
            advance_applied[emp.id] = 0.0
            applied = 0.0

        # The line was built from the REQUESTED figure, but the service
        # caps it at the remaining balance and returns nothing at all
        # when the period was already recovered. Correct the payslip to
        # what actually happened, or it claims a deduction the employee
        # never had — but only when there is a tracked advance behind it,
        # so a hand-typed number with no advance keeps showing as before.
        if advance_input is None and abs(applied - advance) > 0.005:
            delta = round(advance - applied, 2)
            line.advance_deduction = applied
            line.net = round(float(line.net) + delta, 2)
            if "amount_paid" not in inputs or inputs["amount_paid"] in (None, ""):
                line.amount_paid = line.net
                accrued = 0.0
            else:
                accrued = round(float(line.net) - float(line.amount_paid), 2)
            net = float(line.net)
            amount_paid = float(line.amount_paid)
            db.session.flush()

        if accrued > 0.005:
            accruals_to_create.append((emp, line, accrued))

        total_gross += gross
        total_net += net
        total_paid_cash += amount_paid
        total_accrued += accrued

    # MARSOUD-TKT-PAYROLL-JE-BALANCE-GUARD (2026-08-31) — catch the
    # negative-net silent-skip bug at the source. Every line with
    # net < 0 (or payable < 0 after advance) used to be quietly
    # dropped from the Cr side while its (negative) net stayed in
    # total_net, which reduced salary_debit — so Cr > Dr and the
    # payroll run posted with a broken JE. Now we refuse loudly,
    # naming every offending employee + the exact numbers, so HR
    # can fix the input (usually an over-aggressive absence policy)
    # before retrying. Same failure mode motivated by run 42 for
    # company 8, ticket dated 2026-08-31.
    negative_lines = [
        (emp, line, round(float(line.net or 0)
                          + advance_applied.get(emp.id, 0.0), 2))
        for emp, line in lines_created
        if round(float(line.net or 0)
                 + advance_applied.get(emp.id, 0.0), 2) < -0.005
    ]
    if negative_lines:
        details = "؛ ".join(
            f"{emp.name} (صافي {line.net:.2f}، مستحق {payable:.2f})"
            for emp, line, payable in negative_lines
        )
        # Roll back the transaction so the flushed PayrollRun +
        # PayrollLine + EmployeeAccrual rows don't survive as orphans
        # in the DB after the caller sees LedgerError. Without this,
        # SQLite/postgres both keep the rows in the pending transaction
        # until the caller commits or rolls back, and a caller that
        # catches LedgerError but doesn't rollback (very common in
        # audit tests) leaves ghost rows for the next call to trip on.
        db.session.rollback()
        raise LedgerError(
            "لا يمكن ترحيل كشف الرواتب لأن الموظفين التاليين صافيهم "
            f"سالب (الخصومات تتجاوز إجمالي الأجر): {details}. "
            "راجع الغياب/الخصومات أو سياسة الغياب قبل إعادة التشغيل."
        )

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
    # Dr 5210 (single aggregated expense line)
    # Cr each employee's sub-account by their full NET (not just accrued)
    #
    # MARSOUD-ADVANCES — an advance instalment recovered this month is a
    # settlement of what the employee owes, NOT a reduction of the salary
    # expense. So the payable credit (and the expense debit) is net PLUS
    # the recovered instalment, while only `net` is actually paid out.
    # That's what amortises the debit the disbursement left on the
    # employee's 2130 leaf:
    #   -1000 (advance) + 5000 (accrual) - 4500 (payout) = -500 still owed
    # Only the tracked portion counts, so companies with no advances get
    # byte-identical journals to before.
    total_advance_recovered = round(sum(advance_applied.values()), 2)
    # MARSOUD-OPS-HUB-EXPANSION-01 (Phase 1) — sum the three
    # withholding buckets across all lines so we can emit ONE credit
    # each to 2135 / 2136 (employee side) and ONE debit-credit pair
    # to 5217 / 2135 (employer share). Same visual grammar as the
    # single aggregated 5210 debit line above.
    total_employee_insurance = round(
        sum(float(l.insurance_deduction or 0)
            for _, l in lines_created), 2)
    total_income_tax = round(
        sum(float(l.income_tax_deduction or 0)
            for _, l in lines_created), 2)
    total_employer_insurance = round(
        sum(float(l.employer_insurance_share or 0)
            for _, l in lines_created), 2)
    # Salary expense stays at the pre-withholding gross-side amount
    # (net + advance_recovered + emp_insurance + emp_income_tax).
    # Company expense doesn't shrink because part of the pay was
    # routed to the government instead of the employee.
    salary_debit = round(
        total_net + total_advance_recovered
        + total_employee_insurance + total_income_tax, 2)
    accrual_lines = [
        {"account_id": salary_expense.id,
         "debit": salary_debit, "credit": 0,
         "memo": f"مصروف رواتب {month}/{year}"},
    ]

    # ── MARSOUD-COMM-SETTLE (2026-08-25) — close 2150 when a run pays out
    # commissions ────────────────────────────────────────────────────────
    # A commission is recognised as expense ONCE, when it accrues:
    #     Dr 5280 / Cr 2150   (services/sales_commissions.py)
    # settle_commissions_for_employee then folds the amount into `gross`,
    # so it rides inside `total_net` and therefore inside `salary_debit`
    # above. Before this block nothing ever debited 2150, which left two
    # defects at once:
    #   · 2150 accrued forever, never closing against commissions that had
    #     genuinely been paid;
    #   · the same commission hit the P&L twice — once in 5280 at accrual,
    #     again inside 5210 at payout.
    # Reported on company 8: 2150 stood at 8000 with 4800 of it already
    # paid out through payroll.
    #
    # Dr 2150 closes the liability; Cr 5210 backs the duplicate out of
    # salary expense. Cash and the employee's 2130 leaf are deliberately
    # untouched — the rep is still paid through the normal net-salary
    # legs; this pair only corrects the classification.
    #
    # Written as two explicit lines rather than netting `salary_debit`
    # down, so both 5210 and 2150 statements show the movement. Same
    # reasoning as MARSOUD-PAYROLL-LEDGER-03 above, which shows an
    # employee both legs even when they cancel to zero.
    total_commissions = round(
        sum(float(l.commissions or 0) for _, l in lines_created), 2)
    if total_commissions > 0.005:
        commission_liab = get_account_by_code(company_id, "2150")
        if not commission_liab:
            # A commission can only exist if 2150 existed when it accrued
            # (record_commission_for_invoice refuses without it), so this
            # means the account was deleted underneath us. Refuse loudly:
            # posting the run without this pair would silently re-create
            # the double-count this block exists to prevent.
            raise LedgerError(
                "حساب عمولات المبيعات المستحقة (2150) غير موجود — "
                "لا يمكن تصفية العمولات ضمن كشف الرواتب")
        accrual_lines.append({
            "account_id": commission_liab.id,
            "debit": total_commissions, "credit": 0,
            "memo": f"تصفية عمولات مبيعات {month}/{year}",
        })
        accrual_lines.append({
            "account_id": salary_expense.id,
            "debit": 0, "credit": total_commissions,
            "memo": "تخفيض مصروف الرواتب بمقدار العمولة المسجلة سابقاً في 5280",
        })
    # Track sub-accounts so the settlement entry can reuse them
    emp_subaccounts = {}
    for emp, line in lines_created:
        payable = round(float(line.net or 0) + advance_applied.get(emp.id, 0.0), 2)
        if payable < 0.005:
            continue   # skip zero-net employees (e.g. all-deducted)
        emp_acct = party_payroll_account(emp)
        emp_subaccounts[emp.id] = emp_acct
        recovered = advance_applied.get(emp.id, 0.0)
        memo = f"استحقاق راتب — {emp.name}"
        if recovered > 0.005:
            memo += f" (منه {recovered:.2f} خصم سلفة)"
        accrual_lines.append({
            "account_id": emp_acct.id, "debit": 0,
            "credit": payable,
            "memo": memo,
        })

    # MARSOUD-OPS-HUB-EXPANSION-01 (Phase 1) — withholding credits.
    # Only emit lines when there's a non-zero total: a company with
    # no configured GOSI/tax rates gets a journal byte-identical
    # to the pre-fix version.
    if total_employee_insurance > 0.005:
        gosi_emp_acc = get_account_by_code(company_id, "2135")
        if not gosi_emp_acc:
            raise LedgerError(
                "حساب 2135 (تأمينات مستحقة) غير موجود — راجع شجرة الحسابات")
        accrual_lines.append({
            "account_id": gosi_emp_acc.id, "debit": 0,
            "credit": total_employee_insurance,
            "memo": f"تأمينات اجتماعية (حصة الموظفين) {month}/{year}",
        })
    if total_income_tax > 0.005:
        tax_acc = get_account_by_code(company_id, "2136")
        if not tax_acc:
            raise LedgerError(
                "حساب 2136 (ضريبة كسب عمل مستحقة) غير موجود — راجع شجرة الحسابات")
        accrual_lines.append({
            "account_id": tax_acc.id, "debit": 0,
            "credit": total_income_tax,
            "memo": f"ضريبة كسب العمل {month}/{year}",
        })
    if total_employer_insurance > 0.005:
        # Employer's share — Dr 5217 / Cr 2135 (same entry).
        # Both accounts already resolved above (or resolved here
        # if the employee-side withholding was zero).
        gosi_co_acc = get_account_by_code(company_id, "5217")
        gosi_emp_acc = get_account_by_code(company_id, "2135")
        if not gosi_co_acc or not gosi_emp_acc:
            raise LedgerError(
                "حساب 5217 أو 2135 غير موجود — راجع شجرة الحسابات")
        accrual_lines.append({
            "account_id": gosi_co_acc.id,
            "debit": total_employer_insurance, "credit": 0,
            "memo": f"تأمينات اجتماعية (حصة الشركة) {month}/{year}",
        })
        accrual_lines.append({
            "account_id": gosi_emp_acc.id, "debit": 0,
            "credit": total_employer_insurance,
            "memo": f"مستحق للتأمينات (حصة الشركة) {month}/{year}",
        })

    # MARSOUD-TKT-PAYROLL-JE-BALANCE-GUARD — belt-and-suspenders on
    # top of the negative-net refusal above. Sum every line's debit +
    # credit BEFORE post_journal sees it and raise a specific error
    # if they don't balance. Previously post_journal itself would
    # raise a generic "not balanced" LedgerError — this loses the
    # per-line breakdown that makes triage possible. HR should see
    # WHICH accounts drift, not just "off by X".
    _sum_debit = round(sum(float(l.get("debit", 0) or 0)
                             for l in accrual_lines), 2)
    _sum_credit = round(sum(float(l.get("credit", 0) or 0)
                              for l in accrual_lines), 2)
    if abs(_sum_debit - _sum_credit) > 0.01:
        def _line_row(ln):
            dr = float(ln.get("debit", 0) or 0)
            cr = float(ln.get("credit", 0) or 0)
            label = ln.get("memo") or f"acc#{ln.get('account_id', '?')}"
            return f"  · Dr {dr:>12.2f} / Cr {cr:>12.2f}  ← {label}"
        breakdown = "\n".join(_line_row(l) for l in accrual_lines)
        db.session.rollback()   # protect callers from ghost rows
        raise LedgerError(
            f"القيد غير متوازن قبل الترحيل: مدين {_sum_debit:.2f} ≠ "
            f"دائن {_sum_credit:.2f} (فرق {_sum_debit - _sum_credit:+.2f}). "
            f"البنود:\n{breakdown}"
        )

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


# ─── MARSOUD-TKT-PAYROLL-JE-BALANCE-GUARD (2026-08-31) ────────────
# Delete a PayrollRun completely — reverse its JE(s) AND remove
# the PayrollRun row + every PayrollLine + every EmployeeAccrual
# that hung off it. Before this existed, teams had to reverse the
# JE manually and then delete rows via SQL, which caused the
# ambiguity that motivated this ticket (was run 42 really gone?
# where are its lines? did the reverse settle everything?).
#
# Order matters:
#   1. Reverse the settlement JE (if any) — Dr cash returned +
#      Cr employee subaccount returned.
#   2. Reverse the accrual JE — mirror of the original.
#   3. Delete EmployeeAccrual rows tied to the run.
#   4. Delete PayrollLine rows tied to the run.
#   5. Delete the PayrollRun row.
# All in a single db.session; a rollback undoes everything.
def delete_payroll_run(run, *, actor_id=None):
    """Fully undo a PayrollRun — reverse its JE(s) and remove all
    associated rows. Idempotent: calling twice is safe (second call
    hits an already-gone row and returns None).

    Returns a summary dict:
        {"reversed_je_ids": [...], "deleted_line_count": N,
         "deleted_accrual_count": M}
    """
    from app.models import PayrollLine, EmployeeAccrual
    from app.services.ledger import reverse_journal
    from app.models.journal import JournalEntry

    if run is None:
        return None

    summary = {"reversed_je_ids": [], "deleted_line_count": 0,
               "deleted_accrual_count": 0}
    company_id = run.company_id

    # Reverse the accrual JE + any settlement JE. source_type
    # 'payroll' is the accrual, 'payroll_settlement' is the cash
    # payout. Both point at run.id via source_id.
    # "Not-yet-reversed" is derived: an entry is reversed when some
    # OTHER entry has reversal_of == this.id. Filter via NOT EXISTS
    # so we only reverse the originals still standing.
    already_reversed_ids = {
        r[0] for r in db.session.query(JournalEntry.reversal_of)
        .filter(JournalEntry.company_id == company_id,
                JournalEntry.reversal_of.isnot(None)).all()
    }
    original_jes = [
        je for je in JournalEntry.query
        .filter(JournalEntry.company_id == company_id,
                JournalEntry.source_id == run.id,
                JournalEntry.source_type.in_(
                    ["payroll", "payroll_settlement"]),
                JournalEntry.reversal_of.is_(None))
        .all()
        if je.id not in already_reversed_ids
    ]
    for je in original_jes:
        # reverse_journal signature: (entry_id, created_by=None, *,
        # _domain_bypass=False). Pass entry.id (not the entry) and
        # bypass domain hooks — this ticket owns the deletion flow
        # end-to-end and shouldn't retrigger the ledger service's
        # per-source-type callbacks.
        rev = reverse_journal(je.id, created_by=actor_id,
                              _domain_bypass=True)
        summary["reversed_je_ids"].append(rev.id if rev else None)

    # Delete accruals first (they reference lines)
    accruals = EmployeeAccrual.query.filter_by(source_run_id=run.id).all()
    for a in accruals:
        db.session.delete(a)
    summary["deleted_accrual_count"] = len(accruals)

    # Delete lines
    lines = PayrollLine.query.filter_by(run_id=run.id).all()
    for line in lines:
        db.session.delete(line)
    summary["deleted_line_count"] = len(lines)

    # Delete the run itself
    db.session.delete(run)
    db.session.commit()

    try:
        from app.services.activity import log_action
        log_action(
            action_type="DELETE", entity_type="payroll_run",
            entity_id=run.id,
            entity_label=(f"حذف كشف رواتب #{run.id} + عكس "
                          f"{len(summary['reversed_je_ids'])} قيد"),
            company_id=company_id,
            extra_data=summary,
        )
    except Exception:
        pass
    return summary


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


def reactivate_employee(employee):
    """MARSOUD-EMPLOYEE-ARCHIVE — flip a TERMINATED/SUSPENDED employee
    back to ACTIVE without touching any historical data.

    Clears the termination metadata (date/reason/notes) so a future
    re-termination doesn't inherit stale text — but the payroll runs,
    accruals, leave balances, and history rows all stay put. This is
    the "إعادة إلى العمل" action from the archive page.
    """
    old_status = getattr(employee.status, "value", str(employee.status))
    employee.status = EmployeeStatus.ACTIVE
    employee.is_active = True
    employee.termination_date = None
    employee.termination_reason = None
    employee.termination_notes = None
    db.session.commit()
    try:
        from app.services.activity import log_action
        log_action(action_type="UPDATE", entity_type="employee",
                    entity_id=employee.id,
                    entity_label=f"إعادة تفعيل: {employee.name}",
                    company_id=employee.company_id,
                    extra_data={"old": {"status": old_status},
                                 "new": {"status": "ACTIVE"}})
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

    # MARSOUD-OPS-HUB-EXPANSION-01 (Phase 1) — three new % rates
    # for GOSI + income-tax withholding + employer share (see
    # Employee model columns). Same parse-or-ignore contract as the
    # money fields above.
    for fld in ("basic_salary", "allowances", "deductions",
                "insurance_rate", "income_tax_rate",
                "company_insurance_rate"):
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
