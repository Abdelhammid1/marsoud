# Marsoud — Playwright Ticket Verification Report

Run: 2026-06-08 10:52:33
Result: **58/58 checks passed**
Screenshots: `tests/screenshots/`

| Ticket | Check | Status | Screenshot |
|---|---|---|---|
| T1 | Auto-code generation in chart of accounts | ✅ PASS | t1_account_new.png |
| T2 | Journal entry page + template reuse prefill | ✅ PASS | t2_template_use.png |
| T2b | Journal templates list | ✅ PASS | t2_templates_list.png |
| T3 | Email automation (invoice send actions present) | ✅ PASS | t3_invoices.png |
| T4 | Per-company numbering (vendor bills numbered) | ✅ PASS | t4_numbering.png |
| T5 | Payroll / employee module | ✅ PASS | t5_payroll.png |
| T5b | Add-employee form | ✅ PASS | t5_employee_new.png |
| T6 | Invoices module | ✅ PASS | t6_invoice_new.png |
| T7 | Vendor bill VAT posted (2120) — bill detail | ✅ PASS | t7_vendor_bill_detail.png |
| T8 | Reports overhaul — reports index | ✅ PASS | t8_reports_index.png |
| T8b | Cash-flow report with PDF/Excel export | ✅ PASS | t8_cash_flow_export.png |
| T9 | Fixed assets module | ✅ PASS | t9_assets.png |
| T0 | PDF Arabic rendering — balance sheet export reachable | ✅ PASS | t0_balance_sheet.png |
| T10 | Recurring journals overhaul | ✅ PASS | t10_recurring.png |
| T11 | Cash-flow auto-classification select on journal form | ✅ PASS | t11_cashflow_select.png |
| T12 | User invitations + per-company roles | ✅ PASS | t12_users.png |
| T13 | Configurable reminder thresholds on company edit | ✅ PASS | t13_reminders.png |
| T14 | Payroll run form | ✅ PASS | t14_payroll_run.png |
| ACC-18 | Edit accounts — تعديل link in chart of accounts | ✅ PASS | acc18_edit_link.png |
| ACC-18b | Edit-account form (parent / type / code / nature) | ✅ PASS | acc18_edit_form.png |
| ACC-19 | Vendor-bills date-range filter (من/إلى تاريخ) | ✅ PASS | acc19_date_filter.png |
| ACC-20 | Vendor-bills line-item columns (الوصف/النوع/الحساب) | ✅ PASS | acc20_columns.png |
| MARSOUD-3 | Company settings nav link in sidebar | ✅ PASS | marsoud3_settings_link.png |
| HR-01a | HR home page (directory + departments summary) | ✅ PASS | hr01_home.png |
| HR-01b | Departments list page | ✅ PASS | hr01_departments.png |
| HR-01c | New department form | ✅ PASS | hr01_department_form.png |
| HR-01d | Department dropdown on employee form | ✅ PASS | hr01_dept_dropdown.png |
| HR-02 | Employee form: new personal/contract fields | ✅ PASS | hr02_employee_fields.png |
| HR-04a | Invite form offers hr_manager role | ✅ PASS | hr04_invite_role.png |
| MARSOUD-23a | Logo upload field on company-edit page | ✅ PASS | marsoud23_logo_widget.png |
| ADMIN-01a | Normal user blocked from /admin (403) | ✅ PASS | admin01_403.png |
| ADMIN-01b | Super-admin reaches /admin dashboard | ✅ PASS | admin02_dashboard.png |
| ADMIN-02 | Dashboard metrics rendered | ✅ PASS | admin02_metrics.png |
| ADMIN-03 | Companies list with stats | ✅ PASS | admin03_companies.png |
| ADMIN-03b | Company detail drill-down | ✅ PASS | admin03_company_detail.png |
| ADMIN-03c | Company edit page | ✅ PASS | admin03_company_edit.png |
| ADMIN-04 | Users management cross-company | ✅ PASS | admin04_users.png |
| ADMIN-05 | Audit log with filters | ✅ PASS | admin05_audit.png |
| ADMIN-06 | Impersonation log page | ✅ PASS | admin06_impersonations.png |
| GAP-02 | Dashboard segments active/trial/suspended companies | ✅ PASS | gap02_trial_dashboard.png |
| GAP-03+4 | Company-edit surfaces status + plan | ✅ PASS | gap0304_edit_status_plan.png |
| GAP-05 | Audit page exposes from/to date filters | ✅ PASS | gap05_date_filter.png |
| GAP-07 | Audit unifies platform + journal sources | ✅ PASS | gap07_union.png |
| GAP-08 | Errors page reachable from admin | ✅ PASS | gap08_errors_page.png |
| GAP-08b | Per-company errors page | ✅ PASS | gap08_errors_company.png |
| GAP-02-deep | Resend-invite POST audits + flashes | ✅ PASS | gap02_resend.png |
| HR-04b | HR_MANAGER can reach /hr/ | ✅ PASS | hr04_can_see_hr.png |
| HR-04c-journals | HR_MANAGER → /journals/ returns 403 | ✅ PASS | hr04_403_journals.png |
| HR-04c-invoices | HR_MANAGER → /invoices/ returns 403 | ✅ PASS | hr04_403_invoices.png |
| HR-04c-vendor-bills | HR_MANAGER → /vendor-bills/ returns 403 | ✅ PASS | hr04_403_vendor_bills.png |
| HR-04c-accounts | HR_MANAGER → /accounts/ returns 403 | ✅ PASS | hr04_403_accounts.png |
| HR-04c-reports | HR_MANAGER → /reports/ returns 403 | ✅ PASS | hr04_403_reports.png |
| HR-04d | HR_MANAGER can read /payroll/ (no run) | ✅ PASS | hr04_payroll_read.png |
| HR-01-deep | Created department appears in list | ✅ PASS | hr01_after_create.png |
| HR-03-deep | Cron tick processes contract expiry alerts | ✅ PASS | hr03_cron_tick.png |
| MARSOUD-23-deep | Uploaded logo persists and renders in company-edit preview | ✅ PASS | marsoud23_logo_preview.png |
| MARSOUD-23-email | Email base template emits company logo when set | ✅ PASS | n/a |
| GAP-01-deep | Suspended-company user blocked at login | ✅ PASS | gap01_blocked.png |