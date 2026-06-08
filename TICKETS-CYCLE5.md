# Marsoud — Cycle 5 Tickets (HR Phase 1 + MARSOUD-23)

Cycle 5 ships the first half of the HR module (departments, extended employee
profile, contract-expiry alerts, a new HR_MANAGER role) plus a per-company
logo system that propagates through every customer-facing surface (invoices,
payslips, all emails, and any PDF the platform exports).

It is the foundation for Cycle 6 (HR Phase 2: leave types, attendance
exceptions, leave-request workflow, payroll auto-link). Each Phase-2 ticket
depends on something built here — `Department`, the extended `Employee`,
`HR_MANAGER`, the `/hr` blueprint — so this had to land first and ship clean.

| # | Ticket | Status |
|---|---|---|
| **HR-01** | Department model + CRUD + employee link | ✅ Done |
| **HR-02** | Extend employee profile: national_id, nationality, DOB, gender, manager, contract_end_date, notes | ✅ Done |
| **HR-03** | Contract-expiry alerts via existing cron (30/60-day window) | ✅ Done |
| **HR-04** | `HR_MANAGER` role + `@hr_required` decorator + 403 on financial routes | ✅ Done |
| **MARSOUD-23** | Per-company logo upload + unified header on invoices/emails/PDFs | ✅ Done |

Verification: Playwright suite (`tests/test_tickets_playwright.py`) — **58/58 green**
across the full tenant pass + super-admin pass + Cycle 5 deep checks (HR_MANAGER
returns real 403 on /journals, /invoices, /vendor-bills, /accounts, /reports;
department CRUD round-trip; cron tick emits `contract_alerts`; logo round-trips
from upload → company-edit preview → email render).

---

## HR-01 — Department model + CRUD + employee link

**Built:** New `Department(id, company_id, name, description, manager_employee_id,
is_active, created_at)` model with a `UniqueConstraint(company_id, name)` so
two companies can each have a "Sales" department without colliding. Employees
got a nullable `department_id` FK with an index.

A new `/hr` blueprint gates everything behind `@hr_required` (OWNER / ADMIN /
HR_MANAGER). It exposes:
- `/hr/` — directory + summary
- `/hr/departments` — list (active + archived)
- `/hr/departments/new` and `/hr/departments/<id>/edit`
- `/hr/departments/<id>/delete` — **soft-archives** when employees are attached
  (per spec acceptance: refuse hard delete with members); hard-deletes otherwise

The employee form gained a department dropdown; the payroll index gained a
Department column.

**Verified:** HR-01a/b/c/d direct page checks + HR-01-deep round-trip (create
a department, find it in the list, clean up).

**Key files:** `app/models/department.py`, `app/services/hr.py`
(`create_department`, `update_department`, `delete_or_archive_department`),
`app/routes/hr.py`, `app/templates/hr/`, employee form
(`app/templates/payroll/employee_form.html`), payroll index
(`app/templates/payroll/index.html`).

---

## HR-02 — Extended employee profile

**Built:** Added seven nullable columns to `employees`:

| Column | Type | Notes |
|---|---|---|
| `national_id` | `String(50)` | National ID / residency number. Hidden from public list — only shown on detail page. |
| `nationality` | `String(60)` | Free text. |
| `date_of_birth` | `Date` | `Date` only (no time). |
| `gender` | `String(10)` (enum) | `MALE` / `FEMALE`. |
| `manager_id` | `Integer` (self-FK) | Direct supervisor. Self-reference protected (form filters self out + service guard). |
| `contract_end_date` | `Date` (indexed) | The hook HR-03 watches. |
| `notes` | `Text` | Free-form HR notes. |

A bonus column `contract_alert_last_sent` (`Date`) was added too — HR-03 uses
it as dedup state. The form, detail page, and update-employee service all
write/render the new fields. National ID stays out of the list view but
appears on the detail page (spec acceptance #10).

**Verified:** HR-02 direct check confirms the form renders all the new labels.
The employee profile shows them when set. The migration is additive — every
existing employee row still loads with all-`NULL` HR fields.

**Key files:** `app/models/payroll.py` (`Employee` + `Gender` enum),
`migrations/versions/c7a3f1e0b257_hr_phase1_and_logo.py`,
`app/routes/payroll.py` (`new_employee`, `_hr_form_context`),
`app/services/payroll.py` (`update_employee`),
`app/templates/payroll/employee_form.html`,
`app/templates/payroll/employee_profile.html`.

---

## HR-03 — Contract expiry alerts via the existing cron

**Built:** New `app/services/hr.py::check_expiring_contracts()` finds every
active employee whose `contract_end_date` falls between today and today + 60
days. For each such company, it groups expiring contracts into a single email
to the HR recipients (owners / admins / HR_MANAGERs of that company), tagging
each row red (≤ 30 days) or amber (31–60 days). The dedup state lives on
`employees.contract_alert_last_sent`, so the same employee can't be alerted
twice on the same day.

The function is wired into the existing `/cron/tick` route. The tick payload
gains a `contract_alerts` key with `{checked, emails_sent, deduped_same_day}`.

A new email template `emails/contract_expiry.html` extends the shared base
(which itself was upgraded for MARSOUD-23 — see below — so contract alerts
inherit the company logo automatically).

**Verified:** HR-03-deep moves an existing employee's `contract_end_date` to
+20 days, calls `/cron/tick`, asserts a 200 response with `contract_alerts`
in the JSON. Restored after.

**Key files:** `app/services/hr.py` (`check_expiring_contracts`,
`_hr_recipients`), `app/routes/cron.py`,
`app/templates/emails/contract_expiry.html`.

---

## HR-04 — HR_MANAGER role + `@hr_required` + financial-route block

**Built:** Added `hr_manager` to `ALL_ROLES`, with Arabic label "مدير الموارد
البشرية". The permission map now has three new keys:

| Permission | Granted to |
|---|---|
| `hr.manage` | owner, admin, hr_manager |
| `payroll.view` | owner, admin, accountant, hr_manager, viewer |
| `payroll.accruals` | owner, admin, accountant (split from `payroll.employees` so HR can edit employees but not post settlement journal entries) |

`payroll.employees` (create / edit / terminate) now includes `hr_manager`, so
HR owns the employee lifecycle — but `payroll.accruals` (settling an accrual
posts a journal entry) stays financial-only. Same row, different responsibility.

Two new decorators in `app/services/permissions.py`:
- `@hr_required` — wraps an HR route; aborts with 403 for anyone outside
  {owner, admin, hr_manager}.
- `@forbid_roles(*roles)` — generic 403 guard, kept for future use.

Per spec acceptance #18 (HR_MANAGER must get a **real 403** on
`/journals`, `/invoices`, `/vendor-bills`, `/accounts`, `/reports`), a global
`before_request` hook in `app/__init__.py` aborts 403 when the active role is
`hr_manager` and the endpoint belongs to any financial blueprint. This is
more thorough than per-route decorators — even GET endpoints with no
permission decorator (e.g. list views) get blocked.

The sidebar in `base.html` swaps to an HR-only layout when the user's role
is `hr_manager`: dashboard, HR home, departments, payroll (read-only). They
never see — and never navigate into — financial endpoints they can't access.

The invitation form (`/users`) now lets you invite as `hr_manager` (added
to `INVITABLE_ROLES`).

**Verified:**
- **HR-04a:** invitation form shows the Arabic label "مدير الموارد البشرية".
- **HR-04b:** HR_MANAGER logs in and reaches `/hr/`.
- **HR-04c × 5:** real `HTTP 403` on /journals, /invoices, /vendor-bills,
  /accounts, /reports (Playwright captures the response code, not just
  rendered HTML).
- **HR-04d:** HR_MANAGER can GET `/payroll/`.

**Key files:** `app/services/permissions.py` (`P` table, `hr_required`,
`forbid_roles`, `INVITABLE_ROLES`), `app/__init__.py`
(`block_hr_manager_from_financial` before_request), `app/templates/base.html`
(role-aware sidebar), `app/routes/users.py` (uses `INVITABLE_ROLES`),
`app/routes/payroll.py` (settle route → `payroll.accruals`).

---

## MARSOUD-23 — Per-company logo + unified header

**Problem from abdelhamid:** the invoices delivered by email and downloaded
as PDFs looked plain — and there was no way for a company to brand them
with its own logo. The ask: let every company upload a logo from settings,
then propagate it everywhere a customer sees something from us — emails,
invoice HTML, invoice PDF, payslip, and any other PDF the system exports.

**Built:**
- New column `companies.logo_path` (text, `/static/logos/<company_id>.<ext>`).
  Separate from the legacy `logo_url` so existing data isn't touched.
- Company-edit page (`/companies/<id>/edit`) gained a multipart logo upload
  widget with a preview, accepted formats (PNG / JPG / GIF / WEBP / SVG),
  a 2 MB size cap, and a "Remove logo" checkbox. Validation flashes Arabic
  errors for size / extension issues. On replace, old extensions are cleaned
  up so we don't accumulate `1.png` + `1.jpg`.
- The shared email base (`templates/emails/_base.html`) was rewritten to
  resolve the company across multiple template contexts (invoice / employee /
  invitation / explicit `company`), render the logo when present (with a
  fallback "م" badge when not), and surface the VAT number under the company
  name. Every existing email template (`invoice_sent`, `payment_full`,
  `payment_partial`, `invoice_reminder`, `refund_issued`,
  `credit_note_issued`, `invitation`, `payslip`, plus the new
  `contract_expiry`) inherits this header — no per-template changes needed.
- The invoice HTML view (`templates/invoices/view.html`) got a navy
  gradient header with the same logo / name / VAT layout as the email,
  plus the invoice number on the right. The redundant inner title was
  removed so the page reads cleanly.
- `app/services/export.py::_pdf_header` (the shared header function used by
  every PDF the system generates — invoice, payslip, balance sheet, income
  statement, cash-flow, AR/AP aging, VAT report, fixed assets) now draws
  the company logo on the navy band when `logo_path` is set, shifts the
  company-name text to the right of the logo, and appends "VAT # …" to
  the subline when a tax number is on file. Single function, all PDFs.

**Verified:**
- **MARSOUD-23a** (direct): the upload widget shows on `/companies/<id>/edit`
  with `<input type="file" name="logo_file">` present in the HTML.
- **MARSOUD-23-deep:** upload a placeholder PNG, reload the company-edit
  page, assert the rendered HTML contains `<img src="/static/logos/<id>.png">`
  AND the "إزالة الشعار" remove-checkbox label AND that the static file
  itself is reachable (200). Cleaned up after.
- **MARSOUD-23-email:** render the `contract_expiry` email server-side with
  `company=co` and assert the logo URL appears in the output — proves the
  shared email base picks up the logo regardless of which template extends it.

**Key files:** `app/models/company.py` (`logo_path` column),
`migrations/versions/c7a3f1e0b257_hr_phase1_and_logo.py`,
`app/routes/companies.py` (`_save_logo` helper + edit route),
`app/templates/companies/form.html` (upload widget),
`app/templates/emails/_base.html` (multi-context resolver + logo render),
`app/templates/invoices/view.html` (branded header),
`app/services/export.py` (`_company_logo_disk_path` + extended `_pdf_header`).

---

## Schema changes (one migration)

```
b5f2e3a91143                            (head before Cycle 5)
  └─ c7a3f1e0b257  hr phase 1 + logo
        departments  (id, company_id, name, description,
                      manager_employee_id, is_active, created_at)
        employees.department_id           (FK departments, nullable, indexed)
        employees.national_id             (String 50)
        employees.nationality             (String 60)
        employees.date_of_birth           (Date)
        employees.gender                  (String 10 — enum value)
        employees.manager_id              (self-FK, nullable)
        employees.contract_end_date       (Date, indexed)
        employees.contract_alert_last_sent (Date, dedup state for HR-03)
        employees.notes                   (Text)
        companies.logo_path               (String 300)
```

The migration is idempotent — every column / index / table creation is guarded
by a `sa.inspect()` probe, so re-running against a partially-migrated DB is
safe. FK columns on `employees` were added as plain `Integer` (without an
explicit `ForeignKey` in the batch op) to avoid SQLite's "Constraint must
have a name" error in batch-mode; the ORM models still declare the
relationships, and SQLite doesn't enforce FKs by default anyway.

---

## Production deploy checklist

```bash
git pull origin main
.venv/bin/pip install -r requirements.txt          # no new dependencies; safe to re-sync
FLASK_APP=flask_app.py .venv/bin/flask db upgrade  # applies c7a3f1e0b257
sudo systemctl restart marsoud                      # or however you run it
```

Verify the migration: `FLASK_APP=flask_app.py .venv/bin/flask db current`
should print `c7a3f1e0b257 (head)`.

No data migration is needed — every new column is nullable / has a default,
so existing rows continue to load. No new dependencies in `requirements.txt`.

After the restart:
- Existing OWNER / ADMIN logins are unchanged.
- The **Departments** + **HR** entries appear in the sidebar for OWNER, ADMIN,
  and any newly-invited HR_MANAGER.
- The company-edit page (`/companies/<id>/edit`) now shows the **logo
  upload** widget — uploading there immediately reflects on the next invoice
  download / email send.
- The cron tick (`POST /cron/tick`) now reports a `contract_alerts` key in
  its JSON summary. Set or update `contract_end_date` on an employee and the
  next tick will dispatch alerts to the HR recipients.

---

## Honest audit — first build vs. spec acceptance

This cycle was audited line-by-line against the four HR tickets + MARSOUD-23
acceptance criteria, in the same spirit as the Cycle 4 audit. **One gap** was
caught during the audit and closed before push:

| # | Gap | Spec source | Fix |
|---|---|---|---|
| 1 | HR_MANAGER couldn't add / edit employees because `payroll.employees` was unchanged | HR-04 acceptance #16 ("يصل لصفحات الموظفين والأقسام بالكامل") | Added `hr_manager` to `payroll.employees`. Split out `payroll.accruals` (financial — settles a journal entry) to stay restricted to owner / admin / accountant. |

Everything else was caught and built right the first pass — the test suite
went 58/58 on the first run after the gap fix.

---

## What's queued for Cycle 6

HR Phase 2 (MARSOUD-25) — every ticket builds on what landed here:

- **HR-05** — LeaveType + LeaveBalance + monthly accrual cron (uses
  `hr_required` from HR-04, scoped per-company like everything else)
- **HR-05b** — AttendanceException model + UI (uses the new `Employee` +
  `Department` for navigation)
- **HR-06** — LeaveRequest workflow with auto-creation of
  AttendanceExceptions on approval
- **HR-07** — `run_payroll` reads exceptions automatically (no more manual
  absence / late input)
