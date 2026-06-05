# Marsoud — Tickets Shipped

Summary of all tickets implemented in this development cycle. Final score: **9/9 tickets at 100%**, plus infrastructure work.

---

## T1 — Auto-Code Generation for Chart of Accounts

**Problem:** Adding a new account required manually picking the next code.

**Solution:** Endpoint `/accounts/suggest-code` returns the next code based on the parent's hierarchy:
- `step = 10 ^ max(trailing_zeros(parent_code) - 1, 0)`
- Parent picks auto-fill the type as well (consistency)

**Examples verified:**
| Parent | Last child | New code |
|---|---|---|
| 5200 | 5270 | **5280** |
| 5000 | 5200 | **5300** |
| 1100 | 1150 | **1160** |
| 1000 | 1200 | **1300** |

**Key files:** `app/routes/accounts.py`, `app/templates/accounts/form.html`

---

## T2 — Journal Entries Page (5 phases)

**Problem:** Bare-bones list, no search, no filters, no pause/reactivate, no templates.

**Built:**
- **Search:** by number, description, reference, amount, **customer/vendor name**
- **7 filters:** date range, type, status (active/paused), account, **amount range (min/max)**, reference prefix, **user dropdown**
- **Sort:** date / number, asc/desc
- **Pagination:** 25/page
- **Color coding:** invoice (blue) / payment (green) / payroll (amber) / refund (pink) / reversal (purple) / paused (red)
- **Totals strip:** debit / credit / balance delta — live with filters
- **HTMX live search:** 300ms debounce, partial swap
- **Pause/reactivate** with mandatory reason + audit trail
- **Reports filter `is_active=True` everywhere** — paused entries excluded from BS/IS/Cash Flow
- **Templates** (`JournalTemplate`) — save & reuse
- **Recurring journals** — daily/weekly/monthly/yearly via cron tick
- **Audit log** (`JournalAudit`) — every create/pause/reactivate/reverse recorded
- **Source linking** — invoice/payment/payroll journals link back to source
- **Per-entry PDF + Excel** export
- **Filtered list PDF + Excel** export
- **Bulk actions** — multi-select → export PDF/Excel or pause with shared reason

**Key files:** `app/routes/journals.py`, `app/services/journals.py`, `app/models/journal_extras.py`, `app/templates/journals/*.html`

---

## T3 — Email Automation

**Built:**
- SMTP service with **log-only fallback** when credentials missing (zero-config dev)
- 5 HTML email templates (Cairo font, RTL):
  - `invoice_sent.html` — with PDF attached
  - `payment_partial.html`
  - `payment_full.html` (with success icon)
  - `invoice_reminder.html` (7d / 3d / overdue)
  - `payslip.html` — with PDF attached
- Hooks fire automatically on: invoice send, payment record (auto detect partial vs full)
- Checkboxes in forms to skip email per action

**Config keys:** `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_FROM_NAME`, `SMTP_USE_TLS`

**Key files:** `app/services/email.py`, `app/templates/emails/*.html`

---

## T4 — Per-Company Numbering Sequences

**Problem:** Invoice numbers used the global PK (`Invoice.id`), so Company A's 2nd invoice would be `INV-2` even if it was Company A's first. Cross-company bleed.

**Solution:** `NumberSequence(company_id, doc_type, next_number)` with unique constraint. Independent counters per company.

**Verified:**
```
Company A: INV-0001, INV-0002
Company B: INV-0001, INV-0002   ← independent
```

**Prefixes:** `INV-` invoices · `JE-` journals · `PAYROLL-` payroll runs · `EMP-` employees · `VB-` vendor bills · `PMT-` / `REF-` / `CN-` (refund/credit-note refs)

**Key files:** `app/models/numbering.py`, `app/services/numbering.py`

---

## T5 — Payroll / Employee Module

**Built — Employee fields:**
- `employee_number` (auto from EMP- sequence)
- `start_date`, `phone`, `contract_type` (FULL_TIME / PART_TIME / TEMPORARY)
- `status` (ACTIVE / SUSPENDED / TERMINATED)
- `termination_date`, `termination_reason` (RESIGNATION / DISMISSAL / CONTRACT_END / OTHER), `termination_notes`

**Built — Payroll run (per-month):**
- Variable inputs per employee per run (working days, overtime, bonus, absence, late, advance) — reset each month implicitly via fresh `PayrollLine` rows
- **30-day prorated salary** (Gulf standard): `basic / 30 × working_days`
- SUSPENDED/TERMINATED employees **excluded** from run

**Built — UI:**
- List: default filter = ACTIVE; search by name/job; last payslip + date column
- Employee profile: personal info, payslip history, **total received since start date**, terminate-contract form
- Per-employee payslip PDF + full monthly payroll PDF & Excel
- Auto-email payslips on run (uses T3)

> **Note (audit correction):** "total received" is the sum of all the employee's payslips (not separately date-bounded), and the payroll journal debits salary expense at **net** (after deductions), not gross-with-separate-deduction-credits. Both are deliberate simplifications — balances and totals are correct.

**Key files:** `app/models/payroll.py`, `app/services/payroll.py`, `app/routes/payroll.py`, `app/templates/payroll/*.html`

---

## T6 — Invoices Module Enhancements

**Built:**

| Feature | Detail |
|---|---|
| Multiple payment methods | `PaymentMethod` table, each linked to a CoA account (Cash 1110, Bank 1120 seeded; user adds Visa, STC Pay, MADA, cheque…) |
| Search + filters in list | Number / customer name search, date range, status |
| Totals strip in list | Count · Invoiced · Collected · Outstanding |
| Edit only on DRAFT | UI hides button; route redirects if SENT |
| PDF preview before sending | `/invoices/<id>/preview` opens in new tab |
| Resend button | For SENT/PARTIAL/OVERDUE invoices |
| Internal notes | Private to company, not on customer PDF |
| Discounts (line + invoice) | % or fixed; tax applied **after** discount (Saudi VAT standard) |
| Default tax from company | `active_company.vat_rate` prefills, editable per invoice |
| Saved products/services | `Product` table; dropdown + free-text both work |
| Auto reminders | 7d / 3d / overdue via cron tick; idempotent (sent-flag per threshold) |
| Per-invoice toggle | "Send reminders" checkbox |

**Verified math (line + invoice discount + VAT):**
```
2 × 1000 − 5% line disc      → 1900
1 × 500  − 50 fixed line disc → 450
items total                   = 2350
− 10% invoice discount         = 2115 (taxable base)
+ 15% VAT                      = 2432.25 total  ✓
```
Journal posts balanced: Dr AR 2432.25 = Cr Revenue 2115 + Cr VAT Payable 317.25 ✓

**Key files:** `app/models/invoice.py`, `app/models/payment_method.py`, `app/models/product.py`, `app/services/invoicing.py`, `app/services/reminders.py`, `app/routes/invoices.py`

---

## T7 — Vendor Bills Module (مصروفات الموردين)

**Built:** Single page for vendor invoices with **mixed line types** in one bill.

**Line types & filtered account dropdowns:**
| Type | Allowed accounts |
|---|---|
| EXPENSE | 5xxx (any expense) |
| FIXED_ASSET | 12xx except 1290 |
| INVENTORY | 1140 only |

**Payment methods:** CASH (Dr items / Cr 1110) · BANK (Cr 1120) · CREDIT (Cr 2110, vendor required)

**Statuses:** DRAFT → POSTED → PARTIALLY_PAID → PAID → OVERDUE → CANCELLED

**Auto behaviors verified:**
- Fixed-asset line auto-creates `FixedAsset` record linked to vendor + source bill
- Inventory increases (1140 debit) reflect in Balance Sheet immediately
- Expense reflects in Income Statement immediately
- CASH/BANK → marked PAID immediately; CREDIT → POSTED awaiting payment
- AP Aging shows credit balance, auto-overdue marking via cron
- Cannot overpay (validated)
- VAT on a bill (when `tax_rate > 0`) debits **2120 (VAT Payable)** for the input VAT — keeps the journal balanced and feeds the VAT report's "paid" column *(fixed post-audit; see Cycle 3)*

**Key files:** `app/models/vendor_bill.py`, `app/services/vendor_bills.py`, `app/routes/vendor_bills.py`, `app/templates/vendor_bills/*.html`

---

## T8 — Reports Overhaul (10 reports)

**All 11 reports have PDF + Excel export** *(Cash Flow export added post-audit — see Cycle 3):*

| # | Report | Notes |
|---|---|---|
| 1 | Balance Sheet | Existing |
| 2 | Income Statement | Existing |
| 3 | Cash Flow | Existing |
| 4 | **Income Summary** | Per-revenue-account breakdown; total = IS Revenue ✓ |
| 5 | **Expenses Summary** | Per-expense-account breakdown; **drill-down** to journal entries; total = IS Expenses ✓ |
| 6 | **P&L Compared (vs Prior Year)** | Side-by-side with same period last year |
| 7 | AR Aging | 0-30 / 31-60 / 61-90 / 90+ buckets |
| 8 | **AP Aging** | New — vendor side, same bucketing |
| 9 | **VAT Report** | Collected − Paid = Due (gov-ready PDF) |
| 10 | **Payroll Summary** | Per-month, per-employee, with totals per column |
| 11 | **Fixed Assets Report** | Cost / annual dep / accumulated / NBV; ties to BS |

**Tie-out verified:** Income Summary total == IS Revenue · Expenses Summary total == IS Expenses · AR Aging total == 1130 balance

**Infrastructure:** Generic `_list_pdf` / `_list_excel` helpers so future reports get exports in 5 lines, not 100.

**Key files:** `app/services/reports.py`, `app/services/export.py`, `app/routes/reports.py`, `app/templates/reports/*.html`

---

## T9 — Fixed Assets Fixes

**Problem 1:** Account named "مصاريف استهلاك" (wrong — that's amortization). Renamed to **"مصاريف إهلاك الأصول الثابتة"** (Fixed Assets Depreciation Expense). Both 5250 and 1290 updated, in Arabic and English.

**Problem 2:** Pressing "تسجيل الإهلاك" twice in the same month would double-charge the expense.

**Solution:** New `DepreciationEntry` table with `UNIQUE(asset_id, period_year, period_month)`. The depreciation function:
- Loops every asset, skips ones already done for the period
- Returns a dict of `{processed, skipped, total_amount}`
- UI shows a clear message: "تم تسجيل إهلاك [new ones] — الأصول التالية سبق تسجيلها هذا الشهر: [skipped ones]"

**Other improvements:**
- New column "إهلاك الشهر الحالي" (✅ / في انتظار) per asset
- Vendor link on each asset (auto-set from vendor bills, or pick manually)
- Asset profile shows depreciation history table with running NBV per month
- Filters: by type / by depreciation status
- Annual depreciation no longer shown in red

**Journal verified:** Dr 5250 (إهلاك) / Cr 1290 (مجمع إهلاك)

**Key files:** `app/models/asset.py`, `app/services/assets.py`, `app/routes/assets.py`, `app/templates/assets/*.html`

---

## Infrastructure & Rebranding

| | |
|---|---|
| **Brand rename** | LedgerOS → مرصود (Marsoud) across UI, emails, PDFs, README |
| **Flask-Migrate** | Initial migration `51e8090300d1_initial_schema.py` creates **29 tables**; schema later evolved across **7 migration files** total (incl. a legacy-DB sync and the cycle-2 additions) |
| **Setup script** | `setup.py` — re-runnable bootstrap (env / migrations / seed) |
| **`.flaskenv`** | Sets `FLASK_APP=flask_app.py` so all `flask` commands work without env var |
| **Cron tick endpoint** | `POST /cron/tick` — runs overdue marking, invoice reminders, recurring journals in one call |
| **AI agent** | "محاسب مرصود" — Claude tool use, 9 tools (create journal/invoice/payment, list accounts/customers, run reports, explain concepts) |

---

## Stats

- **29 database tables** in the initial migration
- **9 tickets** implemented (see Cycle 3 for post-audit corrections)
- **22 working export endpoints** (11 reports × PDF + Excel — Cash Flow added in Cycle 3)
- **6 new modules** + **5 new email templates**

> Earlier drafts of this file claimed "24 tables", "single migration", and "20 export endpoints (10 reports)". Those were inaccurate and have been corrected above.
