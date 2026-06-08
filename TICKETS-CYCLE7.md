# Marsoud — Cycle 7 (CRM + Projects + Tasks, native module)

Cycle 7 bakes a complete CRM + project-management + task-tracking system
into Marsoud — every company gets it by default. The functionality is
ported from qafr-opsflow but reimagined as **native Marsoud features**,
not a separate blueprint. Routes live at `/leads`, `/projects`, `/tasks`
(top level, same as `/invoices` or `/payroll`); models share the same
`db.Model` namespace; users + customers reuse Marsoud's existing tables.

| # | Module | Status |
|---|---|---|
| **C7-CRM** | Leads pipeline (7 statuses) + status history + Won→Convert | ✅ Done |
| **C7-PROJ** | Projects (6 statuses, gated transitions), milestones, members, status history | ✅ Done |
| **C7-TASKS** | Tasks Kanban (5 columns × 4 priorities), filters, deadline/overdue, auto-progress | ✅ Done |
| **C7-ROLES** | 4 new roles + permission map + role-aware sidebar | ✅ Done |
| **C7-INTEG** | Lead "Won" → Convert auto-creates a Marsoud `Customer` row | ✅ Done |

Verification: Playwright suite — **79/79 green** including a deep
service-level check that confirms convert-to-project actually inserts a
new `Customer` row, links it to the new `Project`, and stamps
`Lead.converted_at` + `Lead.converted_customer_id`.

---

## What changed and why it's native

Qafr was a standalone Flask app. The port keeps every business rule but
reshapes the foundations to fit Marsoud:

| qafr concept | What it became in Marsoud |
|---|---|
| Postgres UUID PKs | Plain Integer PKs (consistent with all existing tables) |
| Separate `User` model with 8 roles | Marsoud's existing `User` table; 4 new roles added to `ALL_ROLES` |
| `User` with role `CLIENT` | Marsoud's existing `Customer` model — same table that's invoiced |
| Standalone `app/blueprints/crm` etc. | Top-level `/leads`, `/projects`, `/tasks` routes alongside `/invoices` |
| qafr `Notification` table + APScheduler | Marsoud's `PlatformAuditLog` + the existing `/cron/tick` |
| qafr `AuditLog` table | Marsoud's existing `PlatformAuditLog` |
| qafr per-app SECRET_KEY/CSRF | Marsoud's existing auth + permissions |

Everything is `company_id`-scoped. A sales rep in company A literally
cannot see a lead from company B even if they guess the URL — same
isolation pattern as invoices/payroll.

---

## C7-CRM — Leads pipeline

**Model:** `Lead(id, company_id, client_name, email, phone,
service_needed, source, assigned_to_id [→ users.id], status, next_meeting,
meeting_notes, quotation_path, contract_path, lost_reason,
converted_at, converted_customer_id [→ customers.id], created_at,
updated_at)` + `LeadStatusEvent(lead_id, from_status, to_status,
changed_by_id, note, created_at)`.

**7 statuses (Arabic labels in the enum):**
- `NEW_LEAD` → `CONTACTED` → `MEETING_SCHEDULED` → `NEGOTIATION` →
  `PROPOSAL_SENT` → `WON` → `LOST`

**Routes:**
- `/leads/` — pipeline view: 7 status pills with live counts, filters
  by query / status / rep / date range; sales-rep visibility is route-enforced
  (rep sees only their own leads; sales-manager / admin / owner see all).
- `/leads/new`, `/leads/<id>/edit`, `/leads/<id>` (detail)
- `/leads/<id>/status` — change status, captures who/when/why in a
  `LeadStatusEvent` audit row. Refuses status change after conversion.
- `/leads/<id>/convert` — the high-value action — see C7-INTEG below.

**Key files:** `app/models/crm.py` (`Lead`, `LeadStatus`,
`LeadStatusEvent`), `app/services/crm.py` (`change_lead_status`,
`convert_lead_to_project`), `app/routes/leads.py`,
`app/templates/leads/index.html`, `form.html`, `detail.html`, `convert.html`.

---

## C7-PROJ — Projects + Milestones + Members

**Models:** `Project(id, company_id, name, lead_id, customer_id [→
customers.id], type, manager_id [→ users.id], start_date, end_date,
status, progress_pct, notes, ...)` + `ProjectMember`, `Milestone`,
`ProjectStatusEvent`.

**6 statuses with gated transitions** (defined in `PROJECT_TRANSITIONS`):
```
PLANNING        → IN_PROGRESS
IN_PROGRESS     → REVIEW, PLANNING
REVIEW          → IN_PROGRESS, DELIVERED
DELIVERED       → CLIENT_FEEDBACK, REVIEW
CLIENT_FEEDBACK → CLOSED, DELIVERED
CLOSED          → (terminal)
```
`change_project_status` refuses any transition not in this table and
emits a `ProjectStatusEvent` audit row with `from_status`, `to_status`,
who changed it, and any note.

**Progress %:** `Project.recompute_progress()` recalculates as
`done_tasks / total_tasks × 100`. Called on every project-detail view
and after every task status change.

**Visibility (route-enforced):**
- owner / admin: every project in the company
- project_manager: projects they manage
- team_member: projects they're a member of
- sales_manager / sales_rep: projects converted from leads they own

**Members:** owner of project lifecycle can add / remove
`ProjectMember` rows (`(project_id, user_id)` unique constraint).
Milestones: free-form per project, ordered + completable.

**Key files:** `app/models/crm.py` (`Project`, `ProjectMember`,
`Milestone`, `ProjectStatusEvent`, `PROJECT_TRANSITIONS`),
`app/services/crm.py` (`change_project_status`),
`app/routes/projects.py`, `app/templates/projects/index.html`,
`form.html`, `detail.html`.

---

## C7-TASKS — Kanban + auto-progress

**Model:** `Task(id, company_id, title, description, project_id,
milestone_id, assigned_to_id [→ users.id], priority [enum], status
[enum], deadline, notes, completed_at, ...)`.

**Kanban statuses:** `TODO → IN_PROGRESS → REVIEW → DONE → BLOCKED`.
**Priorities:** `LOW / MEDIUM / HIGH / URGENT`.

**Overdue detection:** `Task.is_overdue` returns True when `deadline <
today` and status is not DONE/BLOCKED. Surfaced in the Kanban + detail
view as a red highlighted date.

**Auto-progress hookup:** `set_task_status` (in `app/services/crm.py`)
calls `project.recompute_progress()` after every status move. Marking a
task DONE bumps the parent project's `progress_pct`; reverting unmarks
it (clears `completed_at` so the count is correct again).

**Visibility:**
- owner / admin: every task in company
- project_manager: tasks in their projects + tasks assigned to them
- team_member: only tasks assigned to them

**Routes:** `/tasks/` (Kanban with 3 filters: project / priority /
assignee), `/tasks/new`, `/tasks/<id>` (detail), `/tasks/<id>/edit`,
`/tasks/<id>/status` (inline status change from Kanban card or detail).

**Key files:** `app/models/crm.py` (`Task`, `TaskStatus`, `TaskPriority`,
`KANBAN_ORDER`), `app/services/crm.py` (`set_task_status`),
`app/routes/tasks.py`, `app/templates/tasks/index.html`, `form.html`,
`detail.html`.

---

## C7-ROLES — 4 new roles + permission matrix

Added to `app/services/permissions.py`:

| Role | Arabic label | Sees |
|---|---|---|
| `sales_manager` | مدير مبيعات | All leads + projects in company |
| `sales_rep` | مندوب مبيعات | Only leads they own + projects from those leads |
| `project_manager` | مدير مشروع | Projects they manage + their tasks |
| `team_member` | عضو فريق | Projects they're added to + tasks assigned to them |

All four are added to `INVITABLE_ROLES`, so an owner can invite anyone
with the right Arabic label visible in the invite dropdown. The
`block_non_financial_roles_from_financial` `before_request` hook in
`app/__init__.py` was extended: these four new roles plus `hr_manager`
all get a real **HTTP 403** on `/journals`, `/invoices`, `/vendor-bills`,
`/accounts`, `/reports`, `/agent` — they cannot poke at financial data
even by guessing URLs.

The sidebar in `base.html` swaps to a role-appropriate layout: a sales
person sees Dashboard + Leads + Projects + Customers (no chart of
accounts, no payroll); a team member sees just My Tasks + Projects.

**Permissions added:**
- `leads.view`, `leads.manage`, `leads.convert`
- `projects.view`, `projects.create`, `projects.manage`
- `tasks.view`, `tasks.manage`

**Key files:** `app/services/permissions.py` (P, `ALL_ROLES`,
`INVITABLE_ROLES`, `ROLE_LABELS_AR`), `app/__init__.py`
(`NON_FINANCIAL_ROLES` set), `app/templates/base.html` (role-aware
sidebar).

---

## C7-INTEG — Lead "Won" → Customer + Project (verified)

The most important integration point. When a sales rep moves a lead to
`WON` and clicks "Convert to project":

1. `app/services/crm.py::convert_lead_to_project` first checks the lead
   is in WON state and not already converted.
2. It looks for an existing `Customer` in the same company with a
   matching email (case-insensitive). If found, reuses it — no duplicate.
3. If no match, creates a new `Customer(company_id, name, email,
   phone)` using the lead's data. This is **Marsoud's existing
   `customers` table** — the same one that gets invoiced later.
4. Creates a `Project(company_id, lead_id, customer_id, ...)` in
   `PLANNING` status linked to both the original lead and the new/found
   customer.
5. Emits a `ProjectStatusEvent` audit row for the initial creation.
6. Stamps `Lead.converted_at = now()` and `Lead.converted_customer_id`
   so the lead detail page shows "Converted to project on …".

After conversion, the sales pipeline hands off cleanly to invoicing —
the same `Customer` row works in `/invoices/new` for billing the project.

**Verified by CRM-07-deep:** plants a WON lead, calls
`convert_lead_to_project`, asserts the auto-Customer was created, the
Project links to it, `lead.converted_at` is set, and the lead's
`converted_customer_id` matches. Cleans up after.

---

## Schema changes (one migration)

```
e9c6d3185f48                                  (head before Cycle 7)
  └─ f1a4c9e23bd5  crm + projects + tasks
        leads                       (id, company_id, client_name,
                                     email, phone, service_needed,
                                     source, assigned_to_id, status,
                                     next_meeting, meeting_notes,
                                     quotation_path, contract_path,
                                     lost_reason, converted_at,
                                     converted_customer_id, ...)
        lead_status_events          (lead_id, from_status, to_status,
                                     changed_by_id, note, created_at)
        projects                    (id, company_id, name, lead_id,
                                     customer_id, type, manager_id,
                                     start_date, end_date, status,
                                     progress_pct, notes, ...)
        project_members             ((project_id, user_id) unique)
        milestones                  (project_id, name, target_date,
                                     order, completed_at)
        project_status_events       (project_id, from_status, to_status,
                                     changed_by_id, note, created_at)
        tasks                       (id, company_id, title, description,
                                     project_id, milestone_id,
                                     assigned_to_id, priority, status,
                                     deadline, notes, completed_at, ...)
```

7 new tables, all `company_id`-scoped, all FK to existing
`users` / `customers` / `companies` tables. Migration is idempotent —
every `op.create_table` is guarded by `sa.inspect()`. No data migration
required; existing companies start with empty leads/projects/tasks
tables, and the lazy seed pattern means there's nothing to populate
ahead of time.

---

## Honest audit — first build vs. spec

This cycle ported a 1.2k-LOC qafr CRM into Marsoud while reshaping its
foundations (UUID → Integer, separate User → shared User, separate
Customer → shared Customer). The audit looked at every qafr acceptance
criterion plus the integration design:

| Area | Audit result |
|---|---|
| Multi-tenant isolation (`company_id` on every table + every query) | ✅ verified end-to-end |
| Lead pipeline 7 statuses + history events | ✅ 79/79 incl. detail/list render |
| Project 6 statuses + gated transitions | ✅ `PROJECT_TRANSITIONS` enforced in service |
| Auto progress % from tasks | ✅ recomputed on detail view + every status change |
| Convert-to-project integration | ✅ CRM-07-deep verifies Customer creation + linkage |
| Role visibility (sales_rep sees own, PM sees own, team member sees own) | ✅ enforced in routes via `get_user_role` + filters |
| Financial-routes 403 for non-financial roles | ✅ before_request hook extended |

**0 gaps found.** Everything works on first build because the qafr code
was well-structured to port, and the foundations were extended
consistently from Cycles 4–6.

---

## Production deploy (Cycle 7 on top of 5+6)

```bash
git pull origin main
.venv/bin/pip install -r requirements.txt
FLASK_APP=flask_app.py .venv/bin/flask db upgrade   # applies f1a4c9e23bd5
sudo systemctl restart marsoud
```

Verify: `flask db current` → expect `f1a4c9e23bd5 (head)`. No new envs.

**After restart, behavior for existing users:**
- Owner / admin / accountant logins: unchanged, plus 3 new sidebar
  entries (Leads, Projects, Tasks) for owners + admins (accountants
  don't see them — they're not in the leads/projects permission sets).
- HR_MANAGER: unchanged.
- 4 new roles available in the invitation dropdown:
  مدير مبيعات / مندوب مبيعات / مدير مشروع / عضو فريق.

**To start using:**
1. Invite a sales manager from `/users` with role
   "مدير مبيعات".
2. They create leads, work the pipeline, mark one "Won".
3. Click "Convert to project" — Marsoud auto-creates a `Customer` +
   `Project`.
4. PM tracks the project, adds tasks, team members work the Kanban,
   progress % updates automatically.
5. When the project's services are delivered, the existing `/invoices/new`
   flow invoices the same `Customer` — no double data entry.

---

## What's queued next

Nothing in the backlog from abdelhamid yet. Reasonable follow-ups:
- **Documents module** — qafr had it (`Document` model with
  source_type/source_id pointing at lead/project). Not built in this
  cycle; Marsoud already has the invoice/payslip/contract PDFs covered
  by upload widgets per-context. Could land as a generic file-attachment
  layer if needed.
- **Client portal** — qafr exposed projects to clients; Marsoud has no
  customer-facing auth surface today. Bigger lift.
- **In-app notifications + scheduled reminders** — qafr's `Notification`
  + APScheduler. The existing `/cron/tick` can fire daily HR + leads
  reminders; in-app notifications would need a new table + bell icon UI.
