from flask import Flask, session, g, request, abort, redirect, url_for, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from config import Config

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    migrate.init_app(app, db)

    from app.models import User, Company

    # MARSOUD-POS-ORPHAN-CASCADE (Abdelhamid 2026-07-22) — one-shot
    # zombie sweep + integrity probe on boot. Handles rows left over
    # from bulk-SQL deletes that ran BEFORE the CASCADE FK migration.
    # Gated by MARSOUD_ORPHAN_SWEEP_ON_BOOT (default "1") so tests
    # can opt out when they intentionally seed orphan state.
    import os as _os
    if _os.environ.get("MARSOUD_ORPHAN_SWEEP_ON_BOOT", "1") == "1":
        try:
            with app.app_context():
                from app.services.orphan_sweep import (
                    sweep_orphans, probe_variant_drift,
                )
                sweep_orphans(db.engine)
                probe_variant_drift(db.engine)
        except Exception as _e:
            app.logger.warning(
                "orphan_sweep skipped on boot: %s", _e)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    # MARSOUD-API-V1 — resolve bearer-token requests to a User so that
    # `current_user` works on /api/v1/* without a session cookie. Returns
    # None when there's no `Authorization: Bearer …` header, leaving
    # session-based requests untouched.
    @login_manager.request_loader
    def load_user_from_api_token(req):
        auth = req.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        raw = auth[7:].strip()
        if not raw:
            return None
        try:
            from app.services.api_tokens import verify_token
            tok = verify_token(raw)
        except Exception:
            return None
        return tok.user if tok else None

    from app.routes.auth import bp as auth_bp
    from app.routes.dashboard import bp as dashboard_bp
    from app.routes.companies import bp as companies_bp
    from app.routes.accounts import bp as accounts_bp
    from app.routes.journals import bp as journals_bp
    from app.routes.invoices import bp as invoices_bp
    from app.routes.customers import bp as customers_bp
    from app.routes.vendors import bp as vendors_bp
    from app.routes.assets import bp as assets_bp
    from app.routes.payroll import bp as payroll_bp
    from app.routes.reports import bp as reports_bp
    from app.routes.agent import bp as agent_bp
    from app.routes.cron import bp as cron_bp
    from app.routes.products import bp as products_bp
    from app.routes.payment_methods import bp as pmethods_bp
    from app.routes.vendor_bills import bp as vbills_bp
    from app.routes.recurring_bills import bp as recurring_bills_bp, forecast_bp
    from app.routes.users import bp as users_bp
    from app.routes.invitations import bp as invitations_bp
    from app.routes.superadmin import bp as superadmin_bp
    from app.routes.hr import bp as hr_bp
    from app.routes.leads import bp as leads_bp
    from app.routes.projects import bp as projects_bp
    from app.routes.tasks import bp as tasks_bp
    from app.routes.opsflow_extras import (
        documents_bp, notifications_bp, audit_bp, portal_bp,
    )
    from app.routes.calendar import bp as calendar_bp
    from app.routes.settings_roles import bp as settings_roles_bp
    from app.routes.hr_self_service import (
        hr_ss_bp, portal_emp_bp,
    )
    from app.routes.inventory import bp as inventory_bp
    from app.routes.pos import bp as pos_bp
    from app.routes.api_v1 import bp as api_v1_bp
    from app.routes.settings_api_tokens import bp as settings_api_tokens_bp
    from app.routes.activity_views import (
        admin_activity_bp, settings_activity_bp,
    )
    from app.routes.settings_backup import bp as settings_backup_bp
    from app.routes.party_ledger import bp as party_ledger_bp
    from app.routes.crm import bp as crm_bp
    from app.routes.refunds import bp as refunds_bp
    from app.routes.settings_employee_reports import (
        bp as settings_employee_reports_bp,
    )
    from app.routes.manufacturing import bp as manufacturing_bp
    from app.routes.user_files import bp as user_files_bp
    from app.routes.evaluations import bp as evaluations_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(companies_bp, url_prefix="/companies")
    app.register_blueprint(accounts_bp, url_prefix="/accounts")
    app.register_blueprint(journals_bp, url_prefix="/journals")
    app.register_blueprint(invoices_bp, url_prefix="/invoices")
    app.register_blueprint(customers_bp, url_prefix="/customers")
    app.register_blueprint(vendors_bp, url_prefix="/vendors")
    app.register_blueprint(assets_bp, url_prefix="/assets")
    app.register_blueprint(payroll_bp, url_prefix="/payroll")
    app.register_blueprint(reports_bp, url_prefix="/reports")
    app.register_blueprint(agent_bp, url_prefix="/agent")
    app.register_blueprint(cron_bp, url_prefix="/cron")
    app.register_blueprint(products_bp, url_prefix="/products")
    app.register_blueprint(pmethods_bp, url_prefix="/payment-methods")
    app.register_blueprint(vbills_bp, url_prefix="/vendor-bills")
    app.register_blueprint(recurring_bills_bp, url_prefix="/recurring-bills")
    app.register_blueprint(forecast_bp, url_prefix="/forecast")
    app.register_blueprint(users_bp, url_prefix="/users")
    app.register_blueprint(invitations_bp, url_prefix="/invitations")
    app.register_blueprint(superadmin_bp, url_prefix="/admin")
    app.register_blueprint(hr_bp, url_prefix="/hr")
    app.register_blueprint(leads_bp, url_prefix="/leads")
    app.register_blueprint(projects_bp, url_prefix="/projects")
    app.register_blueprint(tasks_bp, url_prefix="/tasks")
    app.register_blueprint(documents_bp, url_prefix="/docs")
    app.register_blueprint(notifications_bp, url_prefix="/notifications")
    app.register_blueprint(audit_bp, url_prefix="/audit")
    app.register_blueprint(portal_bp, url_prefix="/portal")
    app.register_blueprint(calendar_bp, url_prefix="/calendar")
    app.register_blueprint(settings_roles_bp, url_prefix="/settings/roles")
    app.register_blueprint(hr_ss_bp, url_prefix="/hr/accounts")
    app.register_blueprint(portal_emp_bp, url_prefix="/my")
    app.register_blueprint(inventory_bp, url_prefix="/inventory")
    app.register_blueprint(pos_bp, url_prefix="/pos")
    app.register_blueprint(api_v1_bp, url_prefix="/api/v1")
    app.register_blueprint(settings_api_tokens_bp, url_prefix="/settings/api-tokens")
    app.register_blueprint(admin_activity_bp, url_prefix="/admin/activity")
    app.register_blueprint(settings_activity_bp, url_prefix="/settings/activity")
    app.register_blueprint(settings_backup_bp, url_prefix="/settings/backup")
    app.register_blueprint(party_ledger_bp, url_prefix="/reports/party-ledger")
    app.register_blueprint(crm_bp, url_prefix="/crm")
    app.register_blueprint(refunds_bp, url_prefix="/refunds")
    app.register_blueprint(settings_employee_reports_bp,
                            url_prefix="/settings/employee-reports")
    app.register_blueprint(manufacturing_bp, url_prefix="/manufacturing")
    app.register_blueprint(user_files_bp, url_prefix="/files")
    app.register_blueprint(evaluations_bp, url_prefix="/evaluations")

    # MARSOUD-API-V1 — make sure /api/v1/* abort(...) / unauthorized
    # responses come out as JSON instead of HTML / login redirects.
    from werkzeug.exceptions import HTTPException as _HTTPException
    from flask import jsonify as _jsonify

    def _is_api(path):
        return path.startswith("/api/")

    @app.errorhandler(_HTTPException)
    def _api_http_error(e):
        if _is_api(request.path):
            return _jsonify({"error": e.description or e.name}), e.code
        return e

    # Flask-Login redirects unauthenticated requests to login_view; flip
    # that into a 401 JSON when the path is the API.
    @login_manager.unauthorized_handler
    def _api_unauthorized():
        if _is_api(request.path):
            return _jsonify({"error": "missing or invalid bearer token"}), 401
        from flask import redirect, url_for
        return redirect(url_for("auth.login"))

    # MARSOUD-SAAS-SUBDOMAIN — resolves the tenant from the subdomain
    # (nginx sets X-Tenant-Subdomain for any *.marsoud.com request that
    # isn't marsoud.com/www.marsoud.com themselves). Must run BEFORE
    # load_active_company below, since it writes active_company_id into
    # the session for that function to pick up — no other route code
    # changes needed; the subdomain becomes the source of truth while
    # everything downstream keeps reading session["active_company_id"]
    # exactly like before.
    from app.models.company import RESERVED_SUBDOMAINS

    @app.before_request
    def resolve_tenant_from_subdomain():
        from flask_login import current_user
        from flask import abort
        g.tenant_subdomain = None
        tenant = request.headers.get("X-Tenant-Subdomain")
        if not tenant or tenant in RESERVED_SUBDOMAINS:
            return
        company = Company.query.filter_by(subdomain=tenant).first()
        if not company or company.deleted_at is not None:
            abort(404)
        g.tenant_subdomain = tenant
        g.tenant_company = company
        if current_user.is_authenticated:
            if company not in current_user.companies:
                abort(403)
            session["active_company_id"] = company.id

    @app.before_request
    def load_active_company():
        from flask_login import current_user
        from flask import abort
        from app.services.superadmin import IMPERSONATION_SESSION_KEY
        g.active_company = None
        g.user_companies = []
        g.impersonating = False
        if current_user.is_authenticated:
            # MARSOUD — skip soft-deleted companies for everyone except
            # super-admin (who needs to see them to restore / wipe).
            is_superadmin = getattr(current_user, "is_superadmin", False)
            non_suspended = [
                c for c in current_user.companies
                if (c.status or "ACTIVE") != "SUSPENDED"
                and (is_superadmin or c.deleted_at is None)
            ]
            g.user_companies = non_suspended
            # Super-admin view-as: hijack active company to the impersonated one
            view_as_id = session.get(IMPERSONATION_SESSION_KEY)
            if view_as_id and getattr(current_user, "is_superadmin", False):
                comp = db.session.get(Company, view_as_id)
                if comp:
                    g.active_company = comp
                    g.impersonating = True
                    # Read-only enforcement: block any non-GET on tenant routes
                    if (request.method != "GET"
                            and request.endpoint
                            and not request.endpoint.startswith("superadmin.")
                            and request.endpoint not in (
                                "auth.logout", "superadmin.view_as_stop")):
                        abort(403, "وضع المعاينة للقراءة فقط")
            if not g.active_company:
                cid = session.get("active_company_id")
                if cid:
                    comp = db.session.get(Company, cid)
                    if comp and comp in non_suspended:
                        g.active_company = comp
                if not g.active_company and non_suspended:
                    g.active_company = non_suspended[0]
                    session["active_company_id"] = g.active_company.id

    # ─── HR-04 + Cycle 7 ── non-financial roles get 403 on financial routes
    FINANCIAL_BLUEPRINTS = (
        "journals.", "invoices.", "vendor_bills.", "accounts.",
        "reports.", "agent.",
    )
    # Roles that must NOT see financial routes — HR / sales / PM / team / client.
    # owner / admin / accountant / ceo / viewer pass through to the route's own gate.
    NON_FINANCIAL_ROLES = frozenset({
        "hr_manager", "sales_manager", "sales_rep",
        "project_manager", "team_member", "client",
    })

    @app.before_request
    def block_non_financial_roles_from_financial():
        from flask_login import current_user
        from flask import abort
        from app.services.permissions import get_user_role
        if not current_user.is_authenticated or not g.get("active_company"):
            return
        endpoint = (request.endpoint or "")
        if not any(endpoint.startswith(p) for p in FINANCIAL_BLUEPRINTS):
            return
        role = get_user_role(current_user.id, g.active_company.id)
        if role in NON_FINANCIAL_ROLES:
            abort(403)

    # MARSOUD-EMAIL-VERIFY (Abdelhamid 2026-07-22) — send
    # PENDING_VERIFICATION users to the "check your email" page on
    # every request except a small allowlist (auth endpoints,
    # static, and the verify flow itself).
    _VERIFY_ALLOWLIST_PREFIXES = (
        "auth.", "static", "public.",  # login, verify, resend, terms
    )

    @app.before_request
    def block_until_email_verified():
        from flask_login import current_user
        if not current_user.is_authenticated:
            return
        # Super-admin, invited users, and legacy accounts (grandfathered
        # by the migration backfill) are unaffected.
        if not getattr(current_user, "is_pending_verification", False):
            return
        endpoint = (request.endpoint or "")
        if any(endpoint.startswith(p) for p in _VERIFY_ALLOWLIST_PREFIXES):
            return
        # AJAX / API: return 403 JSON instead of redirect.
        if request.path.startswith("/api/"):
            from flask import jsonify
            return jsonify({"error": "email verification required"}), 403
        return redirect(url_for("auth.verify_pending"))

    # MARSOUD-58 — sub-item gating enforcement. Block routes whose
    # sub-item is not in the company's plan, regardless of method.
    @app.before_request
    def enforce_subitem_gating():
        from flask_login import current_user
        from flask import abort
        if not current_user.is_authenticated:
            return
        # Super-admins must reach every route to administer.
        if getattr(current_user, "is_superadmin", False):
            return
        company = g.get("active_company")
        if not company:
            return
        try:
            from app.services.plan_gating import (
                endpoint_to_subitem, subitem_allowed,
            )
            si = endpoint_to_subitem(request.endpoint)
        except Exception:
            # Never let the import/helper take the app down — fall through.
            return
        if si and not subitem_allowed(si, company):
            abort(403)

    # TICKET 1 — subscription read-only enforcement.
    # When a company's subscription is past its grace period AND the
    # read-only toggle is on, reject any unsafe-method request unless the
    # endpoint is explicitly exempt (super-admin / auth / cron / employee
    # password change / renewal). GET / HEAD / OPTIONS always pass through.
    @app.before_request
    def enforce_subscription_read_only():
        from flask import flash, redirect, url_for
        from flask_login import current_user
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        if not current_user.is_authenticated:
            return
        company = g.get("active_company")
        if not company:
            return
        try:
            from app.services.subscription import (
                subscription_state, is_endpoint_exempt,
            )
            if is_endpoint_exempt(request.endpoint):
                return
            state = subscription_state(company)
        except Exception:
            # Never let the gating helper break the app — fall through.
            return
        if state["state"] != "read_only":
            return
        # Super-admin always passes (also exempt via prefix above, but
        # double-check via the model attribute).
        if getattr(current_user, "is_superadmin", False):
            return
        # API calls get a JSON 402 instead of flash + redirect.
        if request.path.startswith("/api/"):
            from flask import jsonify as _jsonify
            return _jsonify({"error": "subscription expired (read-only)"}), 402
        flash(
            "اشتراك الشركة منتهي والنظام في وضع القراءة فقط. "
            "جدّد الاشتراك للمتابعة.",
            "error",
        )
        # Redirect to dashboard if we have a referrer, else dashboard.
        return redirect(request.referrer or url_for("dashboard.index"))

    # Client-portal users can only reach /portal/, /notifications/, /auth/, /static/
    CLIENT_ALLOWED_ENDPOINTS = (
        "portal.", "notifications.", "auth.", "static",
        "invitations.",   # accept-invitation pages
    )
    # HR-SS — employees only see their own portal + invariants (notifications,
    # auth, static, invitation acceptance). Everything else 403s.
    EMPLOYEE_ALLOWED_ENDPOINTS = (
        "portal_emp.", "notifications.", "auth.", "static",
        "invitations.",
    )

    @app.before_request
    def confine_client_to_portal():
        from flask_login import current_user
        from flask import abort, redirect, url_for
        from app.services.permissions import get_user_role
        if not current_user.is_authenticated or not g.get("active_company"):
            return
        endpoint = (request.endpoint or "")
        role = get_user_role(current_user.id, g.active_company.id)
        if role == "client":
            if endpoint.startswith(CLIENT_ALLOWED_ENDPOINTS):
                return
            if endpoint == "dashboard.index":
                return redirect(url_for("portal.index"))
            abort(403)
        if role == "employee":
            if endpoint.startswith(EMPLOYEE_ALLOWED_ENDPOINTS):
                return
            if endpoint == "dashboard.index":
                return redirect(url_for("portal_emp.index"))
            abort(403)

    # MARSOUD-SCHEDULE-LAZY-FIRE (Abdelhamid 2026-07-14) — self-heal
    # recurring tasks WITHOUT depending on an external cron scheduler.
    # First authenticated request per company per 15-min window kicks
    # the materializer so any daily schedule whose window includes
    # today gets its task spawned. materialize_due_schedules is
    # idempotent (checks last_generated_date), so a race between
    # multiple workers or a real cron tick can't create duplicates.
    _LAZY_FIRE_THROTTLE_SECS = 900   # 15 minutes
    _LAZY_FIRE_LAST = {}             # {company_id: datetime}

    @app.before_request
    def lazy_fire_schedules():
        from flask_login import current_user
        # Skip static + auth + cron endpoints — they'd add noise
        # without user value.
        endpoint = (request.endpoint or "")
        if (endpoint.startswith(("static", "auth.", "cron."))
                or request.method != "GET"):
            return
        if not (current_user and getattr(
                current_user, "is_authenticated", False)):
            return
        company = g.get("active_company")
        if not company:
            return
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        last = _LAZY_FIRE_LAST.get(company.id)
        if last and (now - last).total_seconds() < _LAZY_FIRE_THROTTLE_SECS:
            return
        # Update the throttle timestamp BEFORE running so a slow fire
        # doesn't get re-triggered by concurrent requests.
        _LAZY_FIRE_LAST[company.id] = now
        try:
            from app.services.task_schedules import materialize_due_schedules
            materialize_due_schedules(company_id=company.id)
        except Exception:
            import logging
            logging.getLogger("marsoud.lazy_fire").exception(
                "lazy schedule fire failed for company %s", company.id,
            )

    @app.context_processor
    def inject_globals():
        from datetime import datetime
        from app.services.permissions import has_permission, get_user_role
        active_company = g.get("active_company")
        current_role = None
        if active_company:
            from flask_login import current_user as _cu
            # `current_user` resolves to None outside a request context
            # (e.g. when the cron pipeline renders a task-notification
            # email from an app-context-only worker). Guard both the
            # None case and the AnonymousUserMixin case.
            if _cu is not None and getattr(_cu, "is_authenticated", False):
                current_role = get_user_role(_cu.id, active_company.id)
        # TICKET 1 — surface subscription state to every template so the
        # banner in base.html knows what (if anything) to display.
        sub_state = None
        if active_company:
            try:
                from app.services.subscription import subscription_state
                sub_state = subscription_state(active_company)
            except Exception:
                sub_state = None
        # MARSOUD-58 — make the sub-item gate available to templates so
        # the sidebar can hide individual links the plan disabled.
        from app.services.plan_gating import subitem_allowed
        def _subitem_allowed_template(endpoint):
            return subitem_allowed(endpoint, active_company)
        # MARSOUD-MC-EMPLOYEE — the sidebar 👤 icon must reflect whether
        # the CURRENT user has an Employee row in the ACTIVE company,
        # not whether users.employee_id is set globally (which broke for
        # anyone owning >1 company).
        my_employee_here = None
        if active_company:
            from flask_login import current_user as _cu
            # Same guard as inject_globals above — the template may be
            # rendered from a cron worker with no request context.
            if _cu is not None and getattr(_cu, "is_authenticated", False):
                from app.models import Employee
                my_employee_here = Employee.query.filter_by(
                    company_id=active_company.id, user_id=_cu.id,
                ).first()
        return {
            "active_company": active_company,
            "user_companies": g.get("user_companies", []),
            "now": datetime.utcnow(),
            "has_permission": has_permission,
            "current_role": current_role,
            "impersonating": g.get("impersonating", False),
            "subscription": sub_state,
            "subitem_allowed": _subitem_allowed_template,
            "my_employee_here": my_employee_here,
        }

    @app.errorhandler(500)
    def _capture_500(e):
        import traceback as _tb
        from flask_login import current_user
        try:
            from app.models import PlatformError
            company_id = g.active_company.id if g.get("active_company") else None
            user_id = current_user.id if current_user.is_authenticated else None
            row = PlatformError(
                company_id=company_id,
                user_id=user_id,
                route=request.path,
                method=request.method,
                status_code=500,
                message=str(e)[:500],
                traceback=_tb.format_exc()[:5000],
                ip_address=request.remote_addr,
            )
            db.session.add(row)
            db.session.commit()
        except Exception:
            db.session.rollback()
        return ("Internal Server Error", 500)

    # ─── Cycle 7 gap-close: audit listeners (Lead/Project/Task edits) ───
    with app.app_context():
        try:
            from app.services.opsflow_extras import init_audit_listeners
            init_audit_listeners()
        except Exception as e:
            app.logger.exception("audit listeners init failed: %s", e)

    # ─── MARSOUD-32: seed permission catalogue + system roles per company
    with app.app_context():
        try:
            from app.services.roles_seed import (
                seed_permissions_catalog, ensure_roles_ready_for_company,
            )
            from app.models import Company
            seed_permissions_catalog()
            for c in Company.query.all():
                ensure_roles_ready_for_company(c.id)
        except Exception as e:
            app.logger.exception("roles seed failed: %s", e)

    # ─── Cycle 7 gap-close: context for the bell-icon unread counter ────
    @app.context_processor
    def inject_notif_count():
        from flask_login import current_user
        try:
            if current_user.is_authenticated:
                from app.services.opsflow_extras import unread_count_for
                # MARSOUD-NOTIF-TENANT-FIX — scope the header bell
                # count to the ACTIVE company only. Without this a
                # user in multiple companies would see notifications
                # from all of them combined.
                active = g.get("active_company")
                cid = active.id if active else None
                return {"notif_unread_count": unread_count_for(
                    current_user.id, company_id=cid)}
        except Exception:
            pass
        return {"notif_unread_count": 0}

    @app.context_processor
    def inject_today_date():
        """MARSOUD-OVERDUE-REMINDER — expose today's date to any
        template that needs to compare against a due_date (e.g. the
        overdue-reminder button on the invoice view). Returned as a
        callable so `today_date()` in the template stays consistent
        with how the timezone helpers are shaped."""
        from datetime import date as _date
        return {"today_date": _date.today}

    @app.template_filter("money")
    def money_filter(value, currency=None):
        if value is None:
            return "0.00"
        try:
            return f"{float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)

    @app.template_filter("mentions")
    def _mentions_filter(text):
        """MARSOUD-MENTIONS — replace `@[Name](user:ID)` tokens with
        styled anchor tags. Registered as a Jinja filter so any
        comment template can render with `{{ c.content|mentions }}`."""
        from app.services.mentions import render_mentions
        return render_mentions(text)

    @app.template_filter("linkify")
    def _linkify_filter(text):
        """MARSOUD-LINKIFY (Abdelhamid 2026-07-16) — auto-detect URLs
        (http://, https://, www.) inside free-text and wrap them in
        clickable `<a>` tags. Escapes everything else with MarkupSafe
        so we don't open an XSS hole on comment/description fields.

        Composable with the `mentions` filter — use
        `{{ text | mentions | linkify }}` to get both @-mentions
        AND clickable URLs on the same string.
        """
        from app.services.linkify import render_linkify
        return render_linkify(text)

    @app.template_filter("company_dt")
    def company_dt_filter(value, fmt="%Y-%m-%d %H:%M:%S", company=None):
        """MARSOUD-TZ-01 — format a stored UTC datetime in the company's
        timezone. Two call shapes:

           {{ x.created_at | company_dt }}
              # picks up g.active_company (normal HTTP requests).

           {{ x.created_at | company_dt(company=inv.company) }}
              # explicit company — required for emails, PDFs, and cron
              # callbacks where g.active_company isn't populated.

        Falls back to the passed-in company, then g.active_company, then
        server-local formatting so a missing company never blows up a
        render."""
        if value is None:
            return "—"
        from app.services.time import to_company_tz_str
        if company is None:
            company = g.get("active_company") if g else None
        return to_company_tz_str(value, company, fmt)

    # MARSOUD-TASK-CREATED-AT (Abdelhamid 2026-07-22) — humanize a
    # datetime as "اليوم" / "أمس" / "قبل N أيام" / "17 Jul 2026" so
    # task cards can show a friendlier created-at line.
    @app.template_filter("relative_date")
    def relative_date_filter(value):
        if value is None:
            return "—"
        from datetime import datetime as _dt, date as _date
        now = _dt.utcnow()
        # Accept both date and datetime; normalize to a date for the
        # day-delta math.
        if isinstance(value, _dt):
            target_date = value.date()
        elif isinstance(value, _date):
            target_date = value
        else:
            return str(value)
        delta_days = (now.date() - target_date).days
        if delta_days == 0:
            return "اليوم"
        if delta_days == 1:
            return "أمس"
        if 2 <= delta_days <= 6:
            return f"قبل {delta_days} أيام"
        # Older than a week: fall back to a compact absolute date.
        return target_date.strftime("%d %b %Y")

    # MARSOUD-62 — Expose company_logo_email_uri so the email base
    # template can embed the logo as a data: URI (email clients can't
    # fetch /static/ relative paths).
    from app.services.email import company_logo_email_uri
    app.jinja_env.globals["company_logo_email_uri"] = company_logo_email_uri

    # MARSOUD-ACTLOG-01 — log every successful GET as a VIEW activity
    # row. Wrapped in try/except so a logging hiccup never blocks the
    # response. Heavy skip-list to keep the table from drowning.
    _VIEW_SKIP_PREFIXES = (
        "/static/", "/cron", "/agent", "/api/",
        "/heartbeat", "/notifications",
    )

    @app.after_request
    def _log_view_activity(response):
        try:
            from flask_login import current_user
            if not current_user or not current_user.is_authenticated:
                return response
            if request.method != "GET":
                return response
            if response.status_code != 200:
                return response
            path = request.path or ""
            for p in _VIEW_SKIP_PREFIXES:
                if path.startswith(p):
                    return response
            from app.services.activity import (
                log_action, extract_entity_from_route,
                view_logging_enabled,
            )
            if not view_logging_enabled():
                return response
            ent = extract_entity_from_route(path)
            log_action(
                action_type="VIEW",
                entity_type=ent.get("entity_type"),
                entity_id=ent.get("entity_id"),
                route=path, method="GET",
            )
        except Exception:
            pass
        return response

    # MARSOUD-PARTY-LEDGER-02 — CLI: backfill old data
    from scripts.backfill_party_ledger import backfill_cli
    app.cli.add_command(backfill_cli)

    # MARSOUD-COMM-ACCRUAL — CLI: fix commission dates
    from scripts.backfill_commission_accrual import (
        backfill_cli as backfill_commission_cli,
    )
    app.cli.add_command(backfill_commission_cli)

    # MARSOUD-BUG (2026-07) — CLI: merge duplicate User rows that share
    # the same email. Cleans up state left over from before the
    # ensure_user_for_employee fix.
    from scripts.merge_duplicate_users import merge_cli
    app.cli.add_command(merge_cli)

    # DUPE-EMPLOYEE FIX (Abdelhamid, 2026-07) — CLI:
    #   flask merge-duplicate-employees              (dry-run)
    #   flask merge-duplicate-employees --apply      (write)
    # Finds Employee rows in the same company that share an email and
    # merges them. Owner-created-himself-as-employee is the canonical
    # case; every FK from child tables (payroll_lines, employee_history,
    # accruals, leave, commissions, daily reports, etc.) is moved to
    # the earliest Employee row before the duplicate is deleted.
    from scripts.merge_duplicate_employees import merge_cli as _mde_cli
    app.cli.add_command(_mde_cli)

    # ASMAA-FIX 2026-07-03 — CLI: re-sync system role permissions.
    # After the P dict is edited (e.g. broadening tasks.manage), run:
    #   flask resync-system-roles
    # to push the new grants into every company's Role rows so
    # existing users get the change without re-invite.
    @app.cli.command("resync-system-roles")
    def _resync_system_roles():
        """Re-sync system roles' permission grants across every company."""
        from app.models import Company
        from app.services.roles_seed import seed_system_roles_for_company
        touched = 0
        for c in Company.query.order_by(Company.id).all():
            seed_system_roles_for_company(c.id)
            touched += 1
        print(f"re-synced {touched} companies")

    # ASMAA-FIX 2026-07-03 (round 3) — one-shot refresh of every DRAFT
    # digest so employees who opened a stale report before this deploy
    # see the new format without waiting for the next cron tick.
    #   flask refresh-draft-digests
    @app.cli.command("refresh-draft-digests")
    def _refresh_draft_digests():
        """Re-run _summarise on every DRAFT report to pick up format changes."""
        from app.models import EmployeeDailyReport, DailyReportStatus
        from app.services.daily_digest import build_digest
        drafts = EmployeeDailyReport.query.filter_by(
            status=DailyReportStatus.DRAFT,
        ).all()
        touched = 0
        for r in drafts:
            try:
                build_digest(r.company_id, r.employee_id, r.report_date)
                db.session.commit()
                touched += 1
            except Exception as e:
                db.session.rollback()
                print(f"  #{r.id} ({r.employee.name if r.employee else '?'}): "
                      f"{type(e).__name__}: {e}")
        print(f"refreshed {touched} / {len(drafts)} DRAFT report(s)")

    # Ibrahim 2026-07-03 TZ-BUG-FIX — one-shot data cleanup for lead
    # activity rows written between TZ-01 deploy (2026-07-02) and the
    # naive-local→UTC fix. Each row's activity_date + follow_up_date
    # were stored in company-local time; this pulls each one back to
    # UTC. Idempotent-ish: use --apply to write; without it does a
    # dry run.
    #   flask fix-crm-activity-tz               (dry-run)
    #   flask fix-crm-activity-tz --apply       (write)
    #   flask fix-crm-activity-tz --since 2026-07-02 --apply
    import click as _click

    @app.cli.command("fix-crm-activity-tz")
    @_click.option("--apply", is_flag=True,
                    help="Actually write; without this it dry-runs.")
    @_click.option("--since", default="2026-07-02",
                    help="Only rows with created_at >= this date "
                          "(default 2026-07-02, the TZ-01 deploy).")
    def _fix_crm_activity_tz(apply, since):
        from datetime import datetime as _dt
        from app.models.crm_expansion import LeadActivity
        from app.models import Company
        from app.services.time import to_utc_from_company
        try:
            since_dt = _dt.strptime(since, "%Y-%m-%d")
        except ValueError:
            print(f"bad --since format: {since!r} (use YYYY-MM-DD)")
            return
        rows = LeadActivity.query.filter(
            LeadActivity.created_at >= since_dt,
        ).order_by(LeadActivity.id).all()
        # Cache company by id so we don't re-fetch per row.
        co_cache = {}

        def _co(cid):
            if cid not in co_cache:
                co_cache[cid] = db.session.get(Company, cid)
            return co_cache[cid]

        adjusted = 0
        for r in rows:
            co = _co(r.company_id)
            if not co:
                continue
            # activity_date is required; follow_up_date optional.
            new_act = to_utc_from_company(r.activity_date, co)
            new_fup = (
                to_utc_from_company(r.follow_up_date, co)
                if r.follow_up_date else None
            )
            act_diff = new_act != r.activity_date
            fup_diff = (new_fup != r.follow_up_date) if r.follow_up_date else False
            if not (act_diff or fup_diff):
                continue
            print(f"  #{r.id} lead={r.lead_id}  "
                    f"activity: {r.activity_date} → {new_act}"
                    + (f"  follow_up: {r.follow_up_date} → {new_fup}"
                        if fup_diff else ""))
            if apply:
                r.activity_date = new_act
                if r.follow_up_date:
                    r.follow_up_date = new_fup
            adjusted += 1
        if apply:
            db.session.commit()
        tag = "APPLIED" if apply else "DRY-RUN"
        print(f"\n{tag}: {adjusted} row(s) needed the offset shift "
                f"(of {len(rows)} rows since {since}).")

    # MARSOUD-COA-REBUILD — CLI: flask check-coa
    @app.cli.command("check-coa")
    def _check_coa():
        """Report missing required accounts across every company."""
        from app.models import Company
        from app.services.coa_guard import verify_coa
        any_missing = False
        for c in Company.query.order_by(Company.id).all():
            missing = verify_coa(c.id)
            if missing:
                any_missing = True
                print(
                    f"  ❌ company #{c.id} {c.name!r}: missing "
                    f"{', '.join(missing)}"
                )
            else:
                print(f"  ✓ company #{c.id} {c.name!r}: all present")
        if not any_missing:
            print("All companies have a complete CoA.")

    return app
