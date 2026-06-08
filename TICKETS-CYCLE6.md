# Marsoud — Cycle 6 Tickets (HR Phase 2)

Cycle 6 ships the rest of the HR module: leave types & balances, attendance
exceptions, the leave-request workflow, and payroll integration. Together
with Cycle 5 (departments, employee profile fields, contract alerts,
HR_MANAGER role) this completes Phases 1+2 of the spec — every Phase-2
ticket builds on something that landed in Cycle 5, so they ship in the
same release.

**The principle the spec wants installed:** *every day of the month counts
as attendance, unless explicitly logged as an exception.* There is no
daily attendance roster — only `AttendanceException` rows. Payroll just
sums those rows back to figure out absence/late deductions.

| # | Ticket | Status |
|---|---|---|
| **HR-05** | `LeaveType` + `LeaveBalance` + monthly accrual cron | ✅ Done |
| **HR-05b** | `AttendanceException` — exception-based attendance | ✅ Done |
| **HR-06** | `LeaveRequest` workflow + auto-create exceptions on approval | ✅ Done |
| **HR-07** | `run_payroll` reads `AttendanceException` rows automatically | ✅ Done |

Verification: Playwright suite — **70/70 green** across the full tenant
pass + super-admin pass + Cycle 5 deep checks + Cycle 6 deep checks
(default seed, monthly accrual cap clamp, duplicate-exception refusal,
leave-request approval round-trip with cancel-restores-state, rest-day
skipping, payroll auto-math correctness).

---

## HR-05 — Leave types + balances + monthly accrual

**Built:** Two new models in `app/models/leave.py`:

- `LeaveType(company_id, name, accrual_per_month, max_balance, is_paid, is_active)`
  with `UniqueConstraint(company_id, name)`. `is_paid` is the lever that
  decides whether HR-06 approval creates an `APPROVED_LEAVE` exception (paid)
  or `UNPAID_LEAVE` exception (no pay).
- `LeaveBalance(employee_id, leave_type_id, year, balance_days, used_days)`
  with `UniqueConstraint(employee_id, leave_type_id, year)`. The
  `remaining_days` property returns `balance - used` — that's what gets
  shown to the HR_MANAGER + checked on request submission.

**Seeding & lifecycle:**
- New companies get four defaults seeded on `companies.new`: سنوية
  (1.75/mo, cap 60, paid), مرضية (1.0/mo, cap 30, paid), طارئة (0.25/mo,
  cap 3, paid), بدون راتب (0.0/mo, cap 0, unpaid).
- New employees get an empty `LeaveBalance` row for each active leave type
  via `ensure_employee_balances()` called from `payroll.new_employee`.
- The `/hr/leave-types` route lazily seeds defaults if the company has
  none — covers existing companies that pre-date this cycle.

**Monthly accrual:** `app/services/leave.py::monthly_leave_accrual()` adds
`accrual_per_month` to every active employee's balance, clamped to
`max_balance`. Wired into `/cron/tick` — fires on the 1st of every month
(or any day if you pass `?force_accrual=1`, useful for manual catch-up).

**Verified:**
- **HR-05a** (direct): `/hr/leave-types` lists the four auto-seeded defaults.
- **HR-05b** (direct): the new-type form exposes accrual, max balance, paid toggle.
- **HR-05c** (direct): `/hr/employees/<id>/leave-balances` renders all four
  with balance/used/remaining columns.
- **HR-05-deep:** seed → set a balance just below cap → trigger accrual →
  asserts the post-accrual value equals `max_balance` and the summary
  reports at least one capped row.

**Key files:** `app/models/leave.py` (`LeaveType`, `LeaveBalance`),
`app/services/leave.py` (`seed_default_leave_types`, `ensure_employee_balances`,
`monthly_leave_accrual`, `create_leave_type`, `update_leave_type`),
`app/routes/hr.py` (`leave_types`, `leave_type_new`, `leave_type_edit`,
`employee_balances`), `app/routes/companies.py` (seed on create),
`app/routes/payroll.py` (`ensure_employee_balances` on new_employee),
`app/routes/cron.py` (monthly accrual hook).

---

## HR-05b — AttendanceException (exception-based attendance)

**Built:** `AttendanceException(company_id, employee_id, date, type,
duration_hours, note, leave_request_id, created_by, created_at)` with
`UniqueConstraint(employee_id, date)` so a given employee can have at most
one exception per day. The `type` is an enum of four values:

| Type | Meaning | Payroll impact |
|---|---|---|
| `ABSENT` | Manual unpaid absence | -1 day |
| `LATE` | Late arrival (uses `duration_hours`) | `-duration_hours / 8` of a day |
| `UNPAID_LEAVE` | From HR-06 approval of an unpaid leave type | -1 day |
| `APPROVED_LEAVE` | From HR-06 approval of a paid leave type | 0 (paid as if worked) |

The `deduction_days()` method on the model computes the per-row contribution
to absence; `attendance_deductions(employee, year, month)` aggregates a
whole month into `{absence_days, late_days, approved_days, has_exceptions}`.

**Routes:**
- `/hr/attendance?year=&month=&employee_id=` — monthly calendar grid
  (one row per employee, one column per day, color-coded dot per type)
  with a details table below.
- `/hr/attendance/new` — HR_MANAGER picks employee + date + type + note
  + optional duration_hours when type=LATE.
- `/hr/attendance/<id>/delete` — refuses to delete exceptions that came
  from an approved `LeaveRequest` (user must cancel the request instead,
  which removes its linked exceptions).

**Spec acceptance — "ينشأ تلقائياً من اعتماد LeaveRequest":** the approval
path in `app/services/leave.py::approve_leave_request` walks the request's
inclusive date range and inserts one exception per working day with
`leave_request_id` set to link them back.

**Spec acceptance — "أيام الراحة تُستثنى من الحساب":** rest days (Friday +
Saturday by default) are skipped during approval — they don't produce an
exception and don't count toward `days_count`. Per-company configurable
weekend days are a Phase-3 add.

**Verified:**
- **HR-05b-attendance / HR-05b-new** (direct): both pages render.
- **HR-05b-deep:** create one exception, attempt a duplicate on the same
  day for the same employee → asserts `LeaveError` is raised.
- Weekend-skip integration is verified by **HR-06-weekend** (below).

**Key files:** `app/models/leave.py` (`AttendanceException`,
`AttendanceExceptionType`), `app/services/leave.py` (`create_exception`,
`delete_exception`, `exceptions_in_period`, `attendance_deductions`),
`app/routes/hr.py` (`attendance`, `attendance_new`, `attendance_delete`),
`app/templates/hr/attendance.html`, `app/templates/hr/attendance_form.html`.

---

## HR-06 — LeaveRequest workflow

**Built:** `LeaveRequest(company_id, employee_id, leave_type_id,
start_date, end_date, days_count, reason, status, reviewed_by, reviewed_at,
review_note, created_by, created_at)` with status enum
`PENDING / APPROVED / REJECTED / CANCELLED`.

**The lifecycle:**

1. **Submit** (`submit_leave_request`): validates non-empty range,
   no overlap with another active request, no clash with a pre-existing
   AttendanceException, and — for paid types only — that the remaining
   balance covers the requested days. Unpaid types skip the balance check
   per spec acceptance #17. Computes `days_count` excluding rest days.

2. **Approve** (`approve_leave_request`): deducts `used_days` on the
   matching `LeaveBalance` (paid types), then creates one
   `AttendanceException` per working day in the range, linked back via
   `leave_request_id`. Type = `APPROVED_LEAVE` for paid, `UNPAID_LEAVE`
   for unpaid.

3. **Reject** (`reject_leave_request`): sets status, records reviewer +
   note. No side effects.

4. **Cancel** (`cancel_leave_request`): if the request was approved,
   deletes every `AttendanceException` with `leave_request_id = req.id`
   AND restores the corresponding `used_days` on the balance. Cancelling
   a PENDING request is a no-op deduction-wise.

**Routes:** `/hr/leave-requests` (list with status filter),
`/hr/leave-requests/new` (form), and three POST endpoints for
approve / reject / cancel that flash an Arabic confirmation message
including the count of exceptions created.

**Verified:**
- **HR-06a / HR-06b** (direct): list + form render.
- **HR-06-deep:** submit a 5-working-day Sun→Thu request, approve it,
  assert exactly 5 exceptions created + balance.used_days = 5, then
  cancel and assert exceptions gone + used_days back to 0.
- **HR-06-weekend:** submit a Fri→Sat-only request, approve it, assert
  **zero** exceptions created and used_days unchanged — proves rest-day
  skip is honored end-to-end.

**Key files:** `app/models/leave.py` (`LeaveRequest`, `LeaveRequestStatus`),
`app/services/leave.py` (`submit_leave_request`, `approve_leave_request`,
`reject_leave_request`, `cancel_leave_request`, `_daterange_days`,
`_is_rest_day`), `app/routes/hr.py` (`leave_requests`, `leave_request_new`,
`leave_request_approve/reject/cancel`),
`app/templates/hr/leave_requests.html`,
`app/templates/hr/leave_request_form.html`.

---

## HR-07 — Payroll reads AttendanceException rows automatically

**Built:** A new helper `app/services/payroll.py::auto_absence_late_for(
employee, year, month)` returns `(absence_amount, late_amount,
has_exceptions)` where the amounts are in the company currency,
computed as `days × (basic_salary / 30)`. When no exceptions exist for
the period it returns `(0, 0, False)` and `run_payroll` falls back to
manual form input (backward-compatible with Cycle-5-era payroll runs).

When exceptions DO exist:
- The form pre-fills the absence + late inputs with the auto values and
  marks the inputs with a `bg-emerald-50` tint plus a tooltip "محسوب
  تلقائياً من سجل الحضور".
- On submit, `run_payroll` compares each submitted value against the
  auto value. If both match within 0.01 → the line's
  `attendance_auto_calculated` is `True`. If the user overrode either
  field → `False`. The flag is stored on `PayrollLine` for audit.

The math:
- `ABSENT` + `UNPAID_LEAVE` → 1 full day each → multiplied by
  `basic_salary / 30` → absence deduction.
- `LATE` → `duration_hours / 8` of a day (clamped 0…1) → multiplied by
  `basic_salary / 30` → late deduction.
- `APPROVED_LEAVE` → 0 deduction. The employee is paid as if working.

**Spec acceptance — "no double-deduction":** `LeaveBalance.used_days` is
deducted **only** at HR-06 approval. HR-07 reads the resulting
`AttendanceException`s to compute salary deductions — it does not touch
the balance again.

**Verified:**
- **HR-07-deep:** plant 2 × ABSENT + 1 × LATE(4h) for the fixture employee
  in the current period → call `auto_absence_late_for` → assert
  `absence == 2 × daily_rate` and `late == 0.5 × daily_rate` (4h ÷ 8h × 1
  day) within 0.01. Cleans up after.

**Key files:** `app/models/payroll.py`
(`PayrollLine.attendance_auto_calculated`),
`app/services/payroll.py` (`auto_absence_late_for`, modified `run_payroll`),
`app/routes/payroll.py` (passes helper into the template),
`app/templates/payroll/run_form.html` (auto-fill prefill + tooltip).

---

## Schema changes (one migration)

```
c7a3f1e0b257                          (head before Cycle 6)
  └─ d8b4e2f17a31  hr phase 2
        leave_types            (id, company_id, name,
                                accrual_per_month, max_balance,
                                is_paid, is_active, created_at)
        leave_balances         (id, employee_id, leave_type_id,
                                year, balance_days, used_days,
                                created_at)
        leave_requests         (id, company_id, employee_id,
                                leave_type_id, start_date, end_date,
                                days_count, reason, status,
                                reviewed_by, reviewed_at,
                                review_note, created_at, created_by)
        attendance_exceptions  (id, company_id, employee_id, date,
                                type, duration_hours, note,
                                leave_request_id, created_by, created_at)
        payroll_lines.attendance_auto_calculated  (Boolean, default false)
```

Idempotent: every table create and column add is guarded by an
`sa.inspect()` probe so the migration is safe to re-run on a partially
migrated DB. The `attendance_exceptions.leave_request_id` FK requires
`leave_requests` to exist first — the migration creates them in the
correct order.

---

## Honest audit — first build vs. spec acceptance

The first build of Cycle 6 had **one gap** caught during the audit and
closed before push:

| # | Gap | Spec source | Fix |
|---|---|---|---|
| 1 | `days_count` and exception creation didn't skip rest days (Fri/Sat) | HR-06 acceptance + HR-05b note ("أيام الراحة تُستثنى من الحساب — لا تُنشئ استثناء ولا تُحتسب") | `_daterange_days()` now skips weekdays in `DEFAULT_REST_WEEKDAYS = {4, 5}` (Fri/Sat). `approve_leave_request` also skips rest days when emitting `AttendanceException`s. New HR-06-weekend deep check verifies a Fri→Sat request produces 0 exceptions. |

Everything else hit spec on first pass — the rest of the suite went 70/70
green on the first run after the gap fix.

---

## Combined Cycle 5 + 6 production deploy

Both cycles ship in the same release. The two migrations
(`c7a3f1e0b257` then `d8b4e2f17a31`) are chained — `flask db upgrade`
applies both:

```bash
git pull origin main
.venv/bin/pip install -r requirements.txt          # no new dependencies; safe to re-sync
FLASK_APP=flask_app.py .venv/bin/flask db upgrade  # applies c7a3f1e0b257 then d8b4e2f17a31
sudo systemctl restart marsoud                      # or however you run it
```

Verify: `FLASK_APP=flask_app.py .venv/bin/flask db current` should print
`d8b4e2f17a31 (head)`. No envs to add, no data migration required.

**Post-restart behavior:**
- Existing OWNER / ADMIN / ACCOUNTANT logins are unchanged.
- HR_MANAGER role (from Cycle 5) gets a new full sidebar — HR home,
  departments, leave requests, leave types, attendance, payroll
  (read-only).
- The `/hr/leave-types` page auto-seeds the four Saudi defaults on first
  visit for any company that doesn't have any yet. Companies created
  after this release get them at creation time.
- New employees auto-get empty `LeaveBalance` rows for every active
  leave type — no manual setup needed.
- The cron tick (`POST /cron/tick`) reports `contract_alerts` (HR-03)
  and `leave_accrual` (HR-05) in its JSON summary. The leave-accrual
  block only fires on the 1st of each month; pass `?force_accrual=1`
  to run it manually.
- The payroll run form now pre-fills absence + late from each
  employee's `AttendanceException` rows for the period, with a green
  tint + Arabic tooltip "محسوب تلقائياً من سجل الحضور". Users can
  still override; doing so flips `PayrollLine.attendance_auto_calculated`
  to `False` for that line.

---

## What's next (HR Phase 3)

Phase 3 of the HR module would add:
- Self-service employee portal (currently `LeaveRequest`s are submitted
  on behalf of employees by HR_MANAGER)
- Per-company weekend configuration (currently Fri/Sat are hardcoded)
- HR-style reports (department headcount, leave usage, absence summary)
- Performance review module

None of those are in the current backlog from abdelhamid — that's a
genuine "ship Phase 1+2, see how it lives" call.
