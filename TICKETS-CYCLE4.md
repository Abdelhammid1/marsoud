# Marsoud — Cycle 4 Tickets + Honest Audit

Production feedback from abdelhamid (`accountant.manasety.ai`) for this cycle was a
Platform Owner Layer: a separate `/admin` panel sitting above the multi-tenant
system so a single person can see every company, watch every action, manage
accounts, and triage real customer issues. Plus a one-line discoverability fix
(MARSOUD-3) where the existing company-settings form had no link in the nav.

The build went out, was audited honestly against the spec, found at 8 gaps short
of 100% on the first pass, and then the 8 gaps were closed and re-verified.

| # | Ticket | Status |
|---|---|---|
| **TICKET-01** | Access control: `is_superadmin` + `/admin` blueprint + `@superadmin_required` (403 elsewhere) | ✅ Done |
| **TICKET-02** | Platform dashboard: cross-tenant metrics + last-logins + activity feed | ✅ Done |
| **TICKET-03** | Companies management: list, drill-down, edit, suspend/activate, delete, view-as | ✅ Done |
| **TICKET-04** | Users management: cross-company list, suspend, reset password, real resend-invite, unlink | ✅ Done |
| **TICKET-05** | Activity & audit log: filterable, unified PLATFORM + JOURNAL sources, date-range | ✅ Done |
| **TICKET-06** | Support tools: view-as (read-only, fully logged) + per-company errors page | ✅ Done |
| **MARSOUD-3** | "إعدادات الشركة" link in the sidebar | ✅ Done |
| **Gap fixes** | 8 spec-acceptance gaps caught in audit and closed before push | ✅ Done |

Verification: Playwright suite (`tests/test_tickets_playwright.py`) — **40/40 green**
across the full tenant pass + super-admin pass + two deep checks (login block
when the company is suspended, audit row after real resend-invite).

---

## TICKET-01 — Access Control

**Built:** Migration `a4e1d2c80042` adds `users.is_superadmin` (default `false`),
`users.last_login_at`, and `users.is_active` (with backfill). A new blueprint
mounted at `/admin` uses its own dark/crimson layout so it can't be confused with
the tenant UI. `@superadmin_required` raises 403 for anyone without the flag.
All admin queries bypass `company_id` filters — they read across every tenant.

**Verified:** ADMIN-01a (demo user → /admin returns 403) and ADMIN-01b (super-admin → dashboard).

**Key files:** `app/services/superadmin.py` (`superadmin_required`), `app/routes/superadmin.py`, `app/templates/admin/base.html`, `migrations/versions/a4e1d2c80042_super_admin_layer.py`

---

## TICKET-02 — Platform Dashboard

**Built:** `/admin/` summarizes the whole platform in four KPI cards (companies
broken down ACTIVE / TRIAL / SUSPENDED, users with 24h + 7d active counts, total
journals, total invoices) plus two live feeds: the last 20 logins (name / email
/ timestamp) and the latest 15 audit events.

**Key files:** `app/services/superadmin.py` (`platform_overview`), `app/templates/admin/dashboard.html`

---

## TICKET-03 — Companies Management

**Built:** `/admin/companies` lists every tenant with users / journals /
invoices counts and last activity. Per-row actions: drill-down to a detail
page with members + activity, edit settings (name, currency, VAT rate, tax
number, status, plan), suspend/activate, view-as, delete.

**Spec acceptance — "إيقاف شركة يمنع مستخدميها من الدخول فوراً":** companies
gained a `status` column (ACTIVE / SUSPENDED / TRIAL — superseding the legacy
`is_active` boolean which is kept in sync). Login now refuses any user whose
companies are *all* suspended, and `load_active_company` skips suspended
companies when picking the active one. Verified by GAP-01-deep: after
suspending company 1, demo user's login is rejected with
*"كل شركاتك موقوفة. تواصل مع مالك المنصة."*

**Plan field:** `companies.plan` (FREE / PRO / ENTERPRISE) added and surfaced on the list and edit form.

**Key files:** `app/routes/superadmin.py` (`companies`, `company_detail`, `company_edit`, `company_toggle`, `company_delete`), `app/templates/admin/companies.html`, `app/templates/admin/company_detail.html`, `app/templates/admin/company_edit.html`, `app/routes/auth.py` (login block), `app/__init__.py` (`load_active_company`), `migrations/versions/b5f2e3a91143_super_admin_gaps.py`

---

## TICKET-04 — Users Management

**Built:** `/admin/users` is one cross-tenant table — name, email, company
badges, last login, status. Per-row actions:

- **Suspend / activate** — flips `users.is_active`; suspended users are bounced at login.
- **Reset password** — admin sets a new password inline.
- **Resend invite** — finds the user's most recent pending `Invitation` and
  actually calls `send_invitation_email(inv, accept_url)` (in dev the URL is
  flashed; in prod the existing email service ships it). Audited as `user_resend_invite`
  with `sent=True/False` in the details. Verified by GAP-02-deep.
- **Unlink from company** — removes the row from the `user_companies` association.

Every action writes a `PlatformAuditLog` row with the executor, target, IP, and timestamp.

**Key files:** `app/routes/superadmin.py` (`users`, `user_toggle`, `user_reset_password`, `user_resend_invite`, `user_unlink`), `app/templates/admin/users.html`

---

## TICKET-05 — Activity & Audit Log

**Built:** `platform_audit_logs` table records actor, action, target company,
target user, IP, details, and timestamp. `/admin/audit` exposes filters by
**company / user / action / date-range** (from/to), capped at 500 rows.

**Spec acceptance — "البناء فوق JournalAudit الموجود حالياً":** the view doesn't
replace `JournalAudit`; it unions both sources. Each row in the table is tagged
with a `PLATFORM` or `JOURNAL` badge so you can tell where the event came from.

**Hooks (in services + routes):**

- `user_login`, `user_resend_invite`
- `journal_created`, `journal_reversed`
- `invoice_created`, `invoice_paid`, `invoice_refunded`
- `vendor_bill_posted`
- `account_edited`
- `company_suspend`, `company_activate`, `company_edit`, `company_delete`
- `user_suspend`, `user_activate`, `user_reset_password`, `user_unlink_from_company`
- `impersonation_start`, `impersonation_end`

**Key files:** `app/routes/superadmin.py` (`audit`), `app/templates/admin/audit.html`, hooks in `app/services/ledger.py`, `app/services/vendor_bills.py`, `app/routes/invoices.py`, `app/routes/accounts.py`, `app/routes/auth.py`

---

## TICKET-06 — Support Tools

**Built two things:**

### View-as (read-only impersonation)
- `/admin/companies/<id>/view-as` opens a session-level impersonation: the
  super-admin sees the tenant UI as if they were in that company, with a
  rose-red banner across the top: *"وضع المعاينة (View-As) — أنت تشاهد بيانات
  X كمالك منصة. التعديل والحفظ معطّلان."*
- Read-only is enforced at the request layer: any non-GET on a tenant route
  while impersonating returns 403. Only `auth.logout` and `superadmin.view_as_stop`
  are allowed POSTs.
- `superadmin_impersonations` rows store who, which company, started_at,
  ended_at, IP, reason. `/admin/impersonations` shows the full history with
  session duration.

### Per-company errors page
- A global 500 handler (in `app/__init__.py`) captures route, method, status,
  message, traceback, IP, user, and company into `platform_errors`.
- `/admin/errors` shows the last 200 errors platform-wide.
- `/admin/companies/<id>/errors` filters to a specific tenant — what you'd open
  when a customer reports "the page broke."

**Key files:** `app/routes/superadmin.py` (`view_as`, `view_as_stop`, `impersonations`, `errors_global`, `errors_for_company`), `app/services/superadmin.py` (`start_impersonation`, `end_impersonation`, `IMPERSONATION_SESSION_KEY`), `app/__init__.py` (read-only enforcement + error handler), `app/templates/base.html` (banner), `app/templates/admin/impersonations.html`, `app/templates/admin/errors.html`, `app/models/platform_audit.py`

---

## MARSOUD-3 — Company Settings Discoverability

**Problem:** The settings page already existed at `/companies/<id>/edit` with
currency, VAT rate, tax number, and reminder thresholds — but there was no link
to it from the sidebar, so users couldn't find it.

**Built:** Added an "⚙️ إعدادات الشركة" link in the sidebar (gated on `owner` /
`admin` role) pointing to the active company's edit page. Highlighted when on
that route.

**Key files:** `app/templates/base.html`

---

## Bootstrap — `make_superadmin.py`

A repo-root script for promoting or creating the first super-admin.

```
.venv/bin/python make_superadmin.py
    → prompts for email + password + name interactively

.venv/bin/python make_superadmin.py --email you@x.com --password '…' --name "Owner"
    → non-interactive (handy in deploy scripts)
```

Idempotent: re-running on the same email promotes the existing user instead of
erroring. No company is attached — the super-admin sits outside the tenant
boundary.

---

## Honest audit — first build vs. spec acceptance

The first build of Cycle 4 went out with **8 gaps** when audited line-by-line
against the spec. All 8 were closed before push.

| # | Gap | Spec source | Fix |
|---|---|---|---|
| 1 | Suspending a company didn't block its users' logins — only `user.is_active` was checked | Ticket #03 acceptance | Added `companies.status` column; `auth.login` and `load_active_company` now respect it |
| 2 | "Resend invite" was a stub — flashed a message but didn't re-send the email | Ticket #04 acceptance | Now looks up the pending `Invitation` and calls `send_invitation_email` for real |
| 3 | No "TRIAL" company state — only ACTIVE / SUSPENDED | Ticket #02 spec | `companies.status` includes TRIAL; dashboard breaks it out |
| 4 | No "plan / package" field on company | Ticket #03 spec | `companies.plan` (FREE / PRO / ENTERPRISE) added |
| 5 | No date-range filter on the audit page | Ticket #05 spec | from/to date inputs added to `/admin/audit` |
| 6 | Audit only captured creates, not edits/reversals/payments | Ticket #05 spec | Hooks added in `reverse_journal`, invoice pay/refund, `post_vendor_bill`, account edit |
| 7 | Audit didn't union with existing `JournalAudit` table | Ticket #05 spec ("البناء فوق JournalAudit") | `/admin/audit` now queries both, tags each row PLATFORM / JOURNAL |
| 8 | No errors page per company | Ticket #06 spec | `platform_errors` table + 500 handler + `/admin/errors` + per-company variant |

Verified by Playwright deep checks GAP-01-deep (login actually blocked when company suspended) and GAP-02-deep (audit row written after real resend-invite POST).

---

## Schema changes (two migrations)

```
5717814e0264                       (head before Cycle 4)
  └─ a4e1d2c80042  super admin layer
        users.is_superadmin (bool, default false)
        users.last_login_at (datetime, nullable)
        users.is_active     (bool, default true)
        platform_audit_logs
        superadmin_impersonations
  └─ b5f2e3a91143  super admin gaps
        companies.status    (ACTIVE / SUSPENDED / TRIAL, default ACTIVE)
        companies.plan      (FREE / PRO / ENTERPRISE, default FREE)
        platform_errors
```

Both are idempotent — every change is guarded by an inspector probe so re-running
against a partially-migrated DB is safe.

---

## Production deploy checklist

```bash
git pull origin main
.venv/bin/pip install -r requirements.txt          # no new deps; safe to re-sync
FLASK_APP=flask_app.py .venv/bin/flask db upgrade  # applies both migrations
sudo systemctl restart marsoud                     # or however you run it
.venv/bin/python make_superadmin.py --email you@x.com --password '…' --name "Owner"
```

Verify with `FLASK_APP=flask_app.py .venv/bin/flask db current` — should print `b5f2e3a91143 (head)`. Log in and you'll be auto-redirected to `/admin/`.
