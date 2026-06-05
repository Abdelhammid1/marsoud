# Marsoud — Cycle 3 Tickets + Honest Audit

More production feedback from abdelhamid (`accountant.manasety.ai`): four tickets,
plus a full code audit of cycles 1 & 2 against the actual source (the earlier docs
claimed a flat "100%" — this section records what was really true and what got fixed).

| # | Ticket | Status |
|---|---|---|
| **ACCOUNTANT-18** | Edit accounts in the chart of accounts | ✅ Done |
| **ACCOUNTANT-17** | Move account 1150 → parent 1200, recode 1150→1250 | ✅ Covered by ACCOUNTANT-18 |
| **ACCOUNTANT-19** | "من تاريخ — إلى تاريخ" filter on the vendor-bills list | ✅ Done |
| **ACCOUNTANT-20** | Show description / type / account columns on the vendor-bills list | ✅ Done |
| **Audit fix** | T2 journal template reuse was broken | ✅ Fixed |
| **Audit fix** | T8 Cash Flow report had no export | ✅ Fixed |
| **Audit fix** | T7 vendor-bill VAT wasn't posted (bill w/ tax was unbalanced) | ✅ Fixed |

---

## ACCOUNTANT-18 — Edit Accounts in the Chart of Accounts

**Problem:** Once an account was created, nothing about it could be changed — users had to go back to the developer for any edit, blocking work.

**Built:** An "تعديل" button next to every account in the tree opens an edit form for all five fields:

1. Name (Arabic + English)
2. Code
3. Parent account — to move the account between classifications
4. Nature (DEBIT / CREDIT)
5. Classification (ASSET / LIABILITY / EQUITY / REVENUE / EXPENSE)

**Validation:**
- **Posted entries → confirm required.** If the account (or any descendant) has journal lines, the form shows how many and requires a confirmation checkbox before saving. Editing never deletes entries — it only changes how they're classified in reports.
- **Duplicate code → rejected.** Saving a code already used by another account in the company is refused with a clear message (the account's own current code is excluded from the check).
- **Classification cascades to children.** Changing an account's type (directly, or by moving it under a new parent) reclassifies **all descendants** automatically, and resets their normal side to the default for the new type.
- **Circular parent → blocked.** An account can't be made its own parent or a child of one of its descendants (enforced both in the dropdown and server-side).
- **Parent picks the type.** Choosing a parent locks the classification to the parent's, guaranteeing a consistent hierarchy.

**Verified (ACCOUNTANT-17 scenario):** moved 1150 from parent 1100 to 1200 and recoded it 1150→1250; cascade, duplicate-code, circular-parent, and confirm-on-posted-entries paths all tested live.

**Key files:** `app/routes/accounts.py` (`edit`, `_descendants`, `_entry_count`), `app/templates/accounts/edit.html`, `app/templates/accounts/index.html`

---

## ACCOUNTANT-19 — Date-Range Filter on Vendor Bills

**Built:** "من تاريخ" / "إلى تاريخ" date inputs in the vendor-bills filter bar, filtering on `issue_date`. Either bound works alone; both combine with the existing search + status filters; invalid/empty dates are ignored gracefully (no crash).

**Key files:** `app/routes/vendor_bills.py` (`index`), `app/templates/vendor_bills/index.html`

---

## ACCOUNTANT-20 — Line-Item Columns on the Vendor Bills List

**Problem:** Users had to open each bill to see what it contained.

**Built:** Three columns added to the list, pulled straight from each bill's line items — الوصف (description), النوع (type, color-coded badge: مصروف / أصل ثابت / مخزون), الحساب (account code + name). Multi-line bills stack their items within the row.

**Key files:** `app/templates/vendor_bills/index.html`

---

## Audit Fixes

### T2 — Journal template "save & reuse"
The `/journals/templates/<id>/use` route passed `prefill_template` to the form, but the form never read it — clicking "استخدام" opened a blank manual entry. The form now prefills the description and renders one line per template line (account / debit / credit / memo) with totals recalculated, plus a "تم التعبئة من القالب" note.
**Key files:** `app/templates/journals/form.html`

### T8 — Cash Flow report export
Cash Flow was the only one of the 11 reports with no PDF/Excel export. Added `export_cash_flow` (operating / investing / financing / net change via the generic `_list_pdf`/`_list_excel` helpers), wired `"cash-flow"` into the `export_report` dispatcher, and added PDF/Excel buttons to the report page. **Now 11/11 reports export.**
**Key files:** `app/services/export.py`, `app/templates/reports/cash_flow.html`

### T7 — Vendor-bill input VAT
A bill with `tax_rate > 0` debited only the item subtotals but credited the funding source for the tax-inclusive total — an unbalanced journal that `post_journal` would reject. Now, when `tax_amount > 0`, the post debits **2120 (VAT Payable)** for the input VAT. This balances the entry and feeds the VAT report's "paid to suppliers" column (which reads 2120 debits).
**Verified:** a 1000 + 15% bill posts Dr 1150 = Cr 1150, with 150 to 2120; VAT report shows `paid = 150`.
**Key files:** `app/services/vendor_bills.py`

---

## Honest audit results (cycles 1 & 2)

The previous docs claimed a flat 100%. Verified against source:

| Ticket | Real status | Note |
|---|---|---|
| T1, T3, T4, T6, T9 | 100% | Accurate as claimed |
| T2 | ~98% | Template reuse fixed; refund/credit-note journals still lack a source back-link on the view page (cosmetic) |
| T5 | ~95% | "total received" not date-bounded; salary journal posts net not gross (see note in TICKETS.md) |
| T7 | 100% | After VAT fix above |
| T8 | 100% | After Cash Flow export fix above |
| Infrastructure | Code fine; docs were wrong | 7 migrations / 29 tables (not "1 migration / 24 tables"); 22 export endpoints (not 20). AI agent's 9 tools are real. |
| T0, T10, T11, T12, T13, T14 (cycle 2) | 100% | All verified — held up exactly as claimed |

### Known remaining minor items (non-breaking)
- Refund / credit-note journals aren't linked back to their source on the journal view page.
- `total_received` and the net-salary journal are deliberate simplifications (above).
- Residual "ledgeros" naming in logger names and the SQLite filename (cosmetic).
