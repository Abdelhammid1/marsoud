# Mobile API contract — `/api/v1/*`

Reference for the Flutter app at `mobile/`. Every endpoint below is
bearer-authenticated unless noted; every response is JSON; every error
is `{"error": "<short_code_or_message>"}` with the right HTTP status.

Company scoping: `?company_id=N` on every request, or omit to use the
caller's first company. A user attempting a `?company_id=N` they aren't
a member of gets 403 `you are not a member of company N`.

Rate limit: per-token, 100 req/min by default (configurable). On 429 the
response includes `Retry-After` header + `retry_after_seconds` in the body.

## Auth (`/api/v1/auth/*`) — no bearer required

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/v1/auth/login` | `{email, password, device_name?}` | `{token, token_id, user, companies:[{id,name,role}], default_company_id}` |
| POST | `/api/v1/auth/logout` | — | `{ok:true}` (bearer required) |
| POST | `/api/v1/auth/change-password` | `{old, new}` | `{ok:true}` (bearer required) |

Error codes on login: `missing_credentials` (400), `invalid_credentials` (401),
`account_locked` (403 + `retry_after_minutes`), `account_inactive` (403),
`no_companies` (403), `all_companies_suspended` (403).

## Me (`/api/v1/me/*`) — same-blueprint as api_v1.py

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/ping` | Sanity check |
| GET | `/api/v1/me` | `{user, company, role, permissions[]}` |
| GET | `/api/v1/me/companies` | `{count, companies}` |
| GET | `/api/v1/me/tasks` | Cross-company assigned tasks |

## My portal (`/api/v1/my/*`) — mirrors the web `/my/*`

Every endpoint here resolves the current user's Employee row in the
active company. A user with no Employee row gets 404
`{"error": "no_employee_record"}`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/my/account` | Full home bundle: employee + payslips + leave + advance + today_checkin + tenure_label |
| POST | `/api/v1/my/account/password` | `{old, new}` → `{ok:true}` |
| GET | `/api/v1/my/payslip/<line_id>` | PDF binary |
| GET | `/api/v1/my/leave` | List own leave requests |
| POST | `/api/v1/my/leave` | `{leave_type_id, start_date, end_date, reason?}` |
| GET | `/api/v1/my/permission` | List own late-permission requests |
| POST | `/api/v1/my/permission` | `{request_date, hours_count, start_time?, end_time?, reason?}` |
| GET | `/api/v1/my/advance` | List own advance requests |
| POST | `/api/v1/my/advance` | `{amount, reason?}` |
| GET | `/api/v1/my/attendance` | Monthly aggregation (checkins, exceptions, permits, balances, remaining pool) |
| POST | `/api/v1/my/attendance/checkin` | `{lat?, lng?}` — GPS optional |
| POST | `/api/v1/my/attendance/checkout` | `{lat?, lng?}` |
| GET | `/api/v1/my/daily-reports` | List all reports |
| GET | `/api/v1/my/daily-reports/<id>` | Detail (rebuilds DRAFT digest on read) |
| POST | `/api/v1/my/daily-reports/<id>/notes` | `{employee_notes}` |
| POST | `/api/v1/my/daily-reports/<id>/submit` | Freeze report |
| GET | `/api/v1/my/custody` | Cash custodies + requests |
| GET | `/api/v1/my/custody/<id>` | Detail |
| POST | `/api/v1/my/custody/request` | `{amount, purpose?, needed_by_date?}` |
| GET | `/api/v1/my/items` | Item custodies + requests + available items |
| GET | `/api/v1/my/items/<id>` | Detail |
| POST | `/api/v1/my/items/request` | `{item_id, purpose?}` |
| GET | `/api/v1/my/archive` | Own archived tasks |
| POST | `/api/v1/my/archive/<task_id>/restore` | Un-archive |
| GET | `/api/v1/my/activity` | Last 90d of own activity + sessions |

## Notifications (`/api/v1/notifications/*`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/notifications` | `?unread_only=1`, `?limit=50` |
| GET | `/api/v1/notifications/unread-count` | `{count}` — cheap poll target |
| POST | `/api/v1/notifications/<id>/read` | Mark one |
| POST | `/api/v1/notifications/read-all` | Bulk mark |

## Follow-up endpoints (planned)

These features exist as HTML routes today but need JSON wrappers before
the Flutter app can call them. Tracked as follow-up tickets on
`feat/mobile-flutter`:

- `api_v1_tasks.py` — tasks CRUD + projects + calendar (extends api_v1)
- `api_v1_crm.py` — leads + activities + contacts + campaigns + analytics
- `api_v1_hr.py` — HR admin (employees, departments, leave/permission/advance approvals, payroll, custody manager, employee reports reader)
- `api_v1_docs.py` — bearer-compatible `POST /docs/upload/<source>/<id>` for receipt/lead uploads
- `api_v1_misc.py` — user_files, support tickets, invitations
