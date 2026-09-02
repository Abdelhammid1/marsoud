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
        # MARSOUD-API-RATE-LIMIT — stash the token id so the api_v1
        # before_request can look up per-token counters without
        # re-hashing the bearer string.
        if tok:
            try:
                g.api_token_id = tok.id
            except Exception:
                pass
        return tok.user if tok else None

    from app.routes.auth import bp as auth_bp
    # MARSOUD-TERMS-CONSENT — public /terms + /privacy pages.
    from app.routes.public import bp as public_bp
    from app.routes.dashboard import bp as dashboard_bp
    from app.routes.companies import bp as companies_bp
    from app.routes.accounts import bp as accounts_bp
    from app.routes.journals import bp as journals_bp
    from app.routes.accounting_ops import bp as accounting_ops_bp
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
    from app.routes.advances import bp as advances_bp
    from app.routes.custody import bp as custody_bp
    from app.routes.item_custody import bp as item_custody_bp
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
    # MARSOUD-MOBILE-FLUTTER — split JSON surface for the Flutter app.
    # api_v1_auth is intentionally its own blueprint (login can't require
    # a bearer). The other two share the same before_request guard via
    # app.services.api_guard.install_api_guard.
    from app.routes.api_v1_auth import bp as api_v1_auth_bp
    from app.routes.api_v1_me import bp as api_v1_me_bp
    from app.routes.api_v1_notifications import bp as api_v1_notif_bp
    from app.routes.api_v1_misc import bp as api_v1_misc_bp
    # MARSOUD-MOBILE-TKT-01 (2026-08-18) — three mobile-only JSON
    # blueprints for Leads / Meetings / Schedules.
    from app.routes.api_v1_mobile_extras import (
        leads_bp as api_v1_leads_bp,
        meetings_bp as api_v1_meetings_bp,
        schedules_bp as api_v1_schedules_bp,
    )
    # MARSOUD-MOBILE-TKT-05 (2026-08-18) — push-token registration.
    from app.routes.api_v1_push import bp as api_v1_push_bp
    from app.routes.settings_api_tokens import bp as settings_api_tokens_bp
    from app.routes.activity_views import (
        admin_activity_bp, settings_activity_bp,
    )
    from app.routes.settings_backup import bp as settings_backup_bp
    # MARSOUD-COMM-DASHBOARD — standalone commissions management
    from app.routes.commissions_admin import bp as commissions_admin_bp
    from app.routes.treasury import bp as treasury_bp
    from app.routes.hr_decisions import bp as hr_decisions_bp
    from app.routes.purchase_orders import bp as purchase_orders_bp
    from app.routes.settings_usage import bp as settings_usage_bp
    from app.routes.party_ledger import bp as party_ledger_bp
    from app.routes.crm import bp as crm_bp
    from app.routes.refunds import bp as refunds_bp
    from app.routes.settings_employee_reports import (
        bp as settings_employee_reports_bp,
    )
    from app.routes.manufacturing import bp as manufacturing_bp
    from app.routes.user_files import bp as user_files_bp
    from app.routes.evaluations import bp as evaluations_bp
    # MARSOUD-HELP-CENTER-01 (Abdelhamid 2026-07-24).
    from app.routes.help import bp as help_bp
    # MARSOUD-SUPPORT-TICKETS-01 (Abdelhamid 2026-07-24).
    from app.routes.support import bp as support_bp
    from app.routes.support_admin import bp as support_admin_bp
    # MARSOUD-RECURRING-INVOICE-01 UI (Abdelhamid 2026-07-24).
    from app.routes.recurring_invoices import bp as recurring_invoices_bp
    # MARSOUD-DUAL-UOM-WEIGHT-01 UI (Abdelhamid 2026-07-24).
    from app.routes.inventory_counts import bp as inventory_counts_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(public_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(companies_bp, url_prefix="/companies")
    app.register_blueprint(accounts_bp, url_prefix="/accounts")
    app.register_blueprint(journals_bp, url_prefix="/journals")
    app.register_blueprint(accounting_ops_bp, url_prefix="/accounting-ops")
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
    app.register_blueprint(advances_bp, url_prefix="/advances")
    app.register_blueprint(custody_bp, url_prefix="/custody")
    app.register_blueprint(item_custody_bp, url_prefix="/items")
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
    # MARSOUD-MOBILE-FLUTTER — mount the mobile-facing surface.
    app.register_blueprint(api_v1_auth_bp, url_prefix="/api/v1/auth")
    app.register_blueprint(api_v1_me_bp, url_prefix="/api/v1/my")
    app.register_blueprint(api_v1_notif_bp, url_prefix="/api/v1/notifications")
    app.register_blueprint(api_v1_misc_bp, url_prefix="/api/v1/misc")
    # MARSOUD-MOBILE-TKT-01 (2026-08-18)
    app.register_blueprint(api_v1_leads_bp,
                            url_prefix="/api/v1/my/leads")
    app.register_blueprint(api_v1_meetings_bp,
                            url_prefix="/api/v1/my/meetings")
    app.register_blueprint(api_v1_schedules_bp,
                            url_prefix="/api/v1/my/schedules")
    # MARSOUD-MOBILE-TKT-05 (2026-08-18)
    app.register_blueprint(api_v1_push_bp,
                            url_prefix="/api/v1/my/push-tokens")
    app.register_blueprint(settings_api_tokens_bp, url_prefix="/settings/api-tokens")
    app.register_blueprint(admin_activity_bp, url_prefix="/admin/activity")
    app.register_blueprint(settings_activity_bp, url_prefix="/settings/activity")
    app.register_blueprint(settings_backup_bp, url_prefix="/settings/backup")
    app.register_blueprint(commissions_admin_bp,
                            url_prefix="/commissions")
    app.register_blueprint(treasury_bp, url_prefix="/treasury")
    app.register_blueprint(hr_decisions_bp, url_prefix="/hr/decisions")
    app.register_blueprint(purchase_orders_bp,
                            url_prefix="/purchase-orders")
    app.register_blueprint(settings_usage_bp, url_prefix="/settings/usage")
    app.register_blueprint(party_ledger_bp, url_prefix="/reports/party-ledger")
    app.register_blueprint(crm_bp, url_prefix="/crm")
    app.register_blueprint(refunds_bp, url_prefix="/refunds")
    app.register_blueprint(settings_employee_reports_bp,
                            url_prefix="/settings/employee-reports")
    app.register_blueprint(manufacturing_bp, url_prefix="/manufacturing")
    app.register_blueprint(user_files_bp, url_prefix="/files")
    app.register_blueprint(evaluations_bp, url_prefix="/evaluations")
    app.register_blueprint(help_bp, url_prefix="/help")
    app.register_blueprint(support_bp, url_prefix="/support")
    app.register_blueprint(support_admin_bp, url_prefix="/support-admin")
    app.register_blueprint(recurring_invoices_bp,
                            url_prefix="/recurring-invoices")
    app.register_blueprint(inventory_counts_bp,
                            url_prefix="/inventory/counts")

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
    # MARSOUD-PORTAL-403-FIX — carve-out for the read-only insights agent.
    # `agent.` entered FINANCIAL_BLUEPRINTS in Cycle 7, when the blueprint
    # held only the accountant agent (which posts journals). MARSOUD-
    # INSIGHTS-AGENT-01 (2026-08-01) then added /agent/insights* behind its
    # own `insights.use` permission, deliberately granted to hr_manager /
    # sales_manager / project_manager because "it can't post journals" —
    # but this hook 403s the whole prefix before that permission is ever
    # consulted, so base.html advertised a link those roles could not open.
    # The insights routes keep their own require_permission gate; only the
    # blanket prefix block is lifted. agent.index / .chat / .clear (the
    # journal-posting accountant agent) stay blocked.
    FINANCIAL_EXEMPT_PREFIXES = ("agent.insights",)

    @app.before_request
    def block_non_financial_roles_from_financial():
        from flask_login import current_user
        from flask import abort
        from app.services.permissions import get_user_role
        if not current_user.is_authenticated or not g.get("active_company"):
            return
        endpoint = (request.endpoint or "")
        if endpoint.startswith(FINANCIAL_EXEMPT_PREFIXES):
            return
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

    # MARSOUD-FEATURE-FLAGS-KILL-SWITCH (Abdelhamid 2026-07-22) —
    # super-admin can disable a whole module runtime from
    # /admin/feature-flags. Super-admins themselves bypass so they
    # can always fix the toggle.
    _FLAG_ALLOWLIST_PREFIXES = (
        "auth.", "static", "public.", "superadmin.", "cron.",
        "portal_emp.", "portal.", "notifications.",
        "help.",   # in-product docs — must stay reachable
        "support.", "support_admin.",   # support channel always up
    )

    @app.before_request
    def enforce_feature_flags():
        from flask_login import current_user
        endpoint = (request.endpoint or "")
        if not endpoint:
            return
        if any(endpoint.startswith(p) for p in _FLAG_ALLOWLIST_PREFIXES):
            return
        if current_user.is_authenticated and getattr(
                current_user, "is_superadmin", False):
            return
        # Blueprint name doubles as the module key.
        module_key = endpoint.split(".", 1)[0]
        try:
            from app.services.feature_flags import (
                is_module_enabled, disabled_reason,
            )
            if is_module_enabled(module_key):
                return
            reason = disabled_reason(module_key)
        except Exception:
            return
        # AJAX / API: JSON with 503.
        if request.path.startswith("/api/"):
            from flask import jsonify
            resp = jsonify({
                "error": "module temporarily unavailable",
                "module": module_key,
                "reason": reason,
            })
            resp.status_code = 503
            return resp
        from flask import render_template
        return render_template(
            "errors/module_disabled.html",
            module_key=module_key, reason=reason,
        ), 503

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

    # MARSOUD-CHOOSE-PLAN (Abdelhamid 2026-07-22) — after email
    # verify, if the owner's active company still has intended_plan_id
    # NULL, nudge them to /choose-plan before dashboard. Owner-only
    # so a team member doesn't get blocked by a task the owner
    # hasn't done yet.
    _CHOOSE_PLAN_ALLOWLIST_PREFIXES = (
        "auth.", "static", "public.", "superadmin.",
        # MARSOUD-SUPPORT-TICKETS-01 — support agents inside Manasty
        # must reach /support-admin/ even when Manasty has no plan
        # set. And any customer must be able to open a ticket even if
        # their onboarding stalled at the choose-plan gate.
        "support.", "support_admin.",
        # MARSOUD-HELP-CENTER-01 — help remains reachable pre-onboarding.
        "help.",
    )

    @app.before_request
    def require_plan_selection():
        from flask_login import current_user
        if not current_user.is_authenticated:
            return
        if getattr(current_user, "is_superadmin", False):
            return
        company = g.get("active_company")
        if not company:
            return
        # Grandfather anyone who already has an assigned plan (legacy
        # signups + super-admin-comped tenants). Only NEW signups
        # (both plan_id AND intended_plan_id NULL) get nudged.
        if company.plan_id or company.intended_plan_id:
            return
        # Only the OWNER of the company is asked. Team members roll
        # through even if their owner hasn't picked yet.
        from app.services.permissions import get_user_role
        role = get_user_role(current_user.id, company.id)
        if role != "owner":
            return
        endpoint = (request.endpoint or "")
        if any(endpoint.startswith(p) for p in _CHOOSE_PLAN_ALLOWLIST_PREFIXES):
            return
        if request.path.startswith("/api/"):
            from flask import jsonify
            return jsonify({"error": "plan selection required"}), 403
        return redirect(url_for("auth.choose_plan"))

    # MARSOUD-TERMS-CONSENT (Abdelhamid 2026-07-22) — when the super-
    # admin bumps `terms_version`, every user whose stored version
    # doesn't match gets redirected to /re-accept-terms on the next
    # request. Users with NULL terms_version (created before this
    # ticket shipped) are also nudged so we build the audit trail.
    _TERMS_ALLOWLIST_PREFIXES = (
        "auth.", "static", "public.", "superadmin.",
        "support.", "support_admin.", "help.",
    )

    @app.before_request
    def require_current_terms_version():
        from flask_login import current_user
        if not current_user.is_authenticated:
            return
        if getattr(current_user, "is_superadmin", False):
            return
        endpoint = (request.endpoint or "")
        if any(endpoint.startswith(p) for p in _TERMS_ALLOWLIST_PREFIXES):
            return
        try:
            from app.services.legal import (
                get_terms_version, has_published_legal,
            )
            # Nothing published yet → don't nag. Prevents fresh
            # installs + every audit fixture (users created directly
            # via ORM, no /register call) from being redirected.
            if not has_published_legal():
                return
            current = get_terms_version()
        except Exception:
            return
        if (current_user.terms_version or "") == current:
            return
        if request.path.startswith("/api/"):
            from flask import jsonify
            return jsonify({
                "error": "terms acceptance required",
                "terms_version": current,
            }), 403
        return redirect(url_for("auth.reaccept_terms"))

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

    # MARSOUD-SUPERADMIN-CONTROL-01 T2 (2026-08-08) — the unified
    # access resolver. Runs AFTER enforce_subitem_gating so the
    # older gate is still a safety net during rollout; both will
    # collapse into one when T3 lands. Uses can_access() — the
    # SAME function visible_nav() calls — so 'شفت الرابط' ⟺
    # 'يفتح' by construction.
    @app.before_request
    def enforce_access():
        from flask_login import current_user
        from flask import render_template, jsonify, abort
        if not current_user.is_authenticated:
            return
        if getattr(current_user, "is_superadmin", False):
            return
        endpoint = request.endpoint or ""
        try:
            from app.services.access import (
                can_access, ACCESS_EXEMPT_PREFIXES,
                REASON_PLATFORM_DISABLED, REASON_PLAN_MODULE,
                REASON_PLAN_FEATURE, REASON_COMPANY_DENIED,
            )
        except Exception:
            return   # helper import failed — fall open
        if endpoint.startswith(ACCESS_EXEMPT_PREFIXES):
            return
        company = g.get("active_company")
        try:
            allowed, reason = can_access(endpoint, current_user, company)
        except Exception:
            app.logger.exception("enforce_access resolver failed")
            return   # fail open
        if allowed:
            return
        # JSON for /api/*, friendly HTML page otherwise.
        if request.path.startswith("/api/"):
            status = 503 if reason == REASON_PLATFORM_DISABLED else 403
            msg_map = {
                REASON_PLATFORM_DISABLED: "الميزة متوقفة مؤقتاً",
                REASON_PLAN_MODULE:       "الميزة غير متاحة في باقتك",
                REASON_PLAN_FEATURE:      "الميزة غير متاحة في باقتك",
                REASON_COMPANY_DENIED:    "الميزة غير متاحة لشركتك",
            }
            return jsonify({"error": msg_map.get(reason, "غير مسموح"),
                             "reason": reason}), status
        if reason == REASON_PLATFORM_DISABLED:
            try:
                from app.services.feature_registry import module_for_endpoint
                from app.services.feature_flags import disabled_reason
                mod = module_for_endpoint(endpoint)
                r = disabled_reason(mod) if mod else None
            except Exception:
                mod, r = None, None
            return render_template(
                "errors/module_disabled.html",
                module_key=mod or endpoint, reason=r), 503
        if reason in (REASON_PLAN_MODULE, REASON_PLAN_FEATURE,
                      REASON_COMPANY_DENIED):
            # Look up module label + which plans include it for the
            # upgrade CTA. Errors here degrade to a bare page.
            module_label = None
            current_plan = None
            required_plans = []
            try:
                from app.services.feature_registry import (
                    module_for_endpoint, get_module,
                )
                mod_code = module_for_endpoint(endpoint)
                if mod_code:
                    mod = get_module(mod_code)
                    if mod:
                        module_label = mod.label_ar
                    # Which plans include this module?
                    from app.cli import PLAN_SEED
                    for cfg in PLAN_SEED:
                        if mod_code in cfg["modules"]:
                            required_plans.append(cfg["name_ar"])
                if company and getattr(company, "intended_plan", None):
                    current_plan = company.intended_plan.name_ar
                elif company and getattr(company, "subscription_plan", None):
                    current_plan = company.subscription_plan.name_ar
            except Exception:
                pass
            return render_template(
                "errors/plan_upgrade_required.html",
                endpoint=endpoint,
                module_label=module_label,
                current_plan=current_plan,
                required_plans=required_plans), 200
        # REASON_PERMISSION → fall through to the route's own
        # @require_permission decorator, which redirects with a
        # flash rather than aborting 403. This preserves the
        # existing UX for role-gated pages (E6 in audit_portal_403
        # explicitly asserts 302, not 403). Route decorators are
        # already correct — my hook only owns plan-vs-platform.
        return

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
        # MARSOUD-PORTAL-403-FIX — help + support + public are invariants in
        # every other before_request gate in this file (_VERIFY_ALLOWLIST,
        # _FLAG_ALLOWLIST, _CHOOSE_PLAN_ALLOWLIST, _TERMS_ALLOWLIST). They
        # were missing here only, so the "?" help icon and the الدعم الفني
        # link rendered by base.html handed portal users a bare 403.
        #
        # `public.` matters most: require_current_terms_version redirects to
        # /re-accept-terms, and that page links to /terms and /privacy —
        # both `public.` endpoints. Without this a portal user is ordered to
        # accept terms they are then forbidden from reading.
        "help.", "support.", "public.",
    )
    # HR-SS — employees only see their own portal + invariants (notifications,
    # auth, static, invitation acceptance). Everything else 403s.
    EMPLOYEE_ALLOWED_ENDPOINTS = (
        "portal_emp.", "notifications.", "auth.", "static",
        "invitations.",
        # MARSOUD-PORTAL-403-FIX — same invariants as the client list
        # (see the note there on why `public.` is not optional), plus
        # user_files: /files/ is the user's OWN folder (scoped by user_id
        # in user_files._get_or_403), so an employee reaching it sees
        # nothing but their own uploads.
        "help.", "support.", "public.", "user_files.",
    )
    # MARSOUD-PORTAL-403-FIX — endpoints that must bounce a confined user
    # to their portal instead of 403-ing. `dashboard.landing` is the site
    # root ("/"): typing the bare domain while logged in as an employee /
    # client is the single most common way into the app, and it used to
    # fall through to abort(403) because only `dashboard.index` (/home)
    # was special-cased.
    _PORTAL_BOUNCE_ENDPOINTS = ("dashboard.index", "dashboard.landing")

    @app.before_request
    def confine_client_to_portal():
        from flask_login import current_user
        from flask import abort, redirect, url_for
        from app.services.permissions import get_user_role
        if not current_user.is_authenticated or not g.get("active_company"):
            return
        # MARSOUD-MOBILE-FLUTTER — the JSON API has its own bearer +
        # rate-limit + per-endpoint scoping gate (see api_v1.py and
        # app/services/api_guard.py). The HTML portal-confinement rule
        # doesn't apply here; without this skip, an employee-role user
        # gets 403 on every /api/v1/my/* call and the mobile app can't
        # start.
        if request.path.startswith("/api/"):
            return
        endpoint = (request.endpoint or "")
        role = get_user_role(current_user.id, g.active_company.id)
        if role == "client":
            if endpoint.startswith(CLIENT_ALLOWED_ENDPOINTS):
                return
            if endpoint in _PORTAL_BOUNCE_ENDPOINTS:
                return redirect(url_for("portal.index"))
            abort(403)
        if role == "employee":
            if endpoint.startswith(EMPLOYEE_ALLOWED_ENDPOINTS):
                return
            if endpoint in _PORTAL_BOUNCE_ENDPOINTS:
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
        from app.services.currency import CURRENCY_ORDER
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
        # MARSOUD-DASH-SHELL (2026-08-04) — route the sidebar gate
        # through endpoint_to_subitem first, exactly as the request-time
        # 403 gate does (see before_request above). Passing the RAW
        # endpoint meant any endpoint absent from SUB_ITEM_CATALOG was
        # hidden from the sidebar for tenants past their subscription
        # window with a non-NULL allowed_subitems — even though the page
        # itself loads fine for them. It was already silently hiding ~18
        # existing rows, accounting_ops.index among them.
        #
        # This can only WIDEN sidebar visibility to match what the
        # request gate already permits, so it cannot produce a 403. The
        # alternative — adding new keys to SUB_ITEM_CATALOG — would 403
        # every existing tenant until a super-admin re-saved each plan.
        from app.services.plan_gating import (
            subitem_allowed, endpoint_to_subitem,
        )
        def _subitem_allowed_template(endpoint):
            si = endpoint_to_subitem(endpoint)
            # None means "no sub-item gates this endpoint" — the
            # before_request gate lets those through untouched
            # (`if si and not subitem_allowed(si)`), so the sidebar must
            # too. Falling back to the raw endpoint here instead would
            # hide exactly the links the user can still open, which is
            # the bug being fixed.
            if si is None:
                return True
            return subitem_allowed(si, active_company)
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
        # MARSOUD-SUPERADMIN-CONTROL-01 T2 (2026-08-08) — expose
        # the unified access resolver so the sidebar template can
        # ask the SAME predicate the request-time guard uses. The
        # old `permission_map.get(endpoint)` + `has_permission(req)`
        # dance in base.html called two different maps, one of
        # which was 10 endpoints short. This closes that gap.
        def _can_access_endpoint(endpoint):
            from flask_login import current_user as _cu2
            if not _cu2 or not getattr(_cu2, "is_authenticated", False):
                return False
            try:
                from app.services.access import can_access
                allowed, _ = can_access(endpoint, _cu2, active_company)
                return allowed
            except Exception:
                # fail open — matches the request-time guard
                return True
        # MARSOUD-PLAN-SSOT (2026-08-17) — expose plan_snapshot as a
        # Jinja global so every template (super-admin, tenant settings,
        # dashboard, etc.) resolves the plan through the SAME helper.
        # Never render Company.plan (the legacy String column,
        # defaulted to "FREE") directly.
        from app.services.plan_snapshot import plan_snapshot
        return {
            "active_company": active_company,
            "user_companies": g.get("user_companies", []),
            "now": datetime.utcnow(),
            "has_permission": has_permission,
            "can_access_endpoint": _can_access_endpoint,
            "current_role": current_role,
            "impersonating": g.get("impersonating", False),
            "subscription": sub_state,
            "plan_snapshot": plan_snapshot,
            "subitem_allowed": _subitem_allowed_template,
            "my_employee_here": my_employee_here,
            # MARSOUD-CURRENCY-AR — every currency <select> in the app
            # renders from this one list instead of its own hardcoded
            # copy, so the dictionary really is the single source.
            "currency_codes": CURRENCY_ORDER,
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

    # ─── MARSOUD-PLAN-BUNDLE-FIXES-01 (2026-08-07) — auto-heal
    # PLAN_SEED drift. When a module is added to PLAN_SEED in code,
    # nothing used to update the DB row until someone remembered to
    # run `flask seed-plans`; result: the module was silently invisible
    # for existing tenants. Now: on boot, we call the same idempotent
    # sync the CLI uses. No-op when in sync; single INFO log line
    # naming the plans that were re-seeded when drift is detected.
    # Wrapped in try/except so a fresh clone before migrations run
    # (plans table missing) still boots cleanly.
    with app.app_context():
        try:
            from app.cli import sync_plans_from_seed
            summary = sync_plans_from_seed()
            if summary["updated"]:
                app.logger.info(
                    "PLAN_SEED drift auto-healed: re-seeded %s",
                    ", ".join(summary["updated"]))
        except Exception:
            app.logger.exception("plan-seed drift check failed")

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

    # MARSOUD-BOT-PROTECTION-01 (Abdelhamid 2026-07-24) — surface the
    # Turnstile site key to the register template. When empty, the
    # template hides the widget entirely (dev mode).
    @app.context_processor
    def inject_turnstile_site_key():
        return {"turnstile_site_key":
                app.config.get("TURNSTILE_SITE_KEY", "")}

    # MARSOUD-SUPPORT-TICKETS-01 — surface whether the current user
    # is a Manasty support agent so the sidebar knows whether to show
    # the /support-admin/ link. The check itself is O(1) per request.
    @app.context_processor
    def inject_is_support_agent():
        try:
            from app.services.support_permissions import is_support_agent
            return {"is_support_agent": is_support_agent()}
        except Exception:
            return {"is_support_agent": False}

    # MARSOUD-HELP-CENTER-01 — surface the module_key for the header
    # "?" icon. Cheap DB lookup guarded by a simple exists check;
    # per-request, so a newly-published article shows up immediately.
    @app.context_processor
    def inject_help_module_key():
        try:
            from app.services.help_media import (
                current_module_key, has_published_article,
            )
            key = current_module_key()
            if key and has_published_article(key):
                return {"help_module_key": key}
        except Exception:
            pass
        return {"help_module_key": None}

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

    # MARSOUD-CURRENCY-AR — the app stores ISO codes but must show Arabic
    # names. One dictionary in app/services/currency.py backs both this
    # filter and the ReportLab exports that can't use a filter.
    @app.template_filter("currency_ar")
    def currency_ar_filter(code):
        from app.services.currency import currency_name_ar
        return currency_name_ar(code)

    # MARSOUD-VBILL-CURRENCY-DISPLAY (2026-08-17) — one shared filter for
    # "amount + currency" rendering. Every vendor-bill / invoice template
    # used to hand-roll `{{ "%.2f"|format(x) }} {{ code|currency_ar }}`,
    # which meant most cells dropped the currency entirely and the ones
    # that kept it disagreed on formatting (%.2f vs {:,.0f}). Now every
    # template calls `{{ amount|amount_ar(currency) }}` and gets a
    # consistent "1,234.50 جنيه مصري" back. Passing currency=None (the
    # default) renders the number only — same behaviour as `money` but
    # with thousand separators, so a bare number stays consistent too.
    @app.template_filter("amount_ar")
    def amount_ar_filter(value, currency=None):
        if value is None:
            return ""
        try:
            formatted = f"{float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)
        if not currency:
            return formatted
        from app.services.currency import currency_name_ar
        name = currency_name_ar(currency)
        return f"{formatted} {name}" if name else formatted

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

    # MARSOUD-CASH-CUSTODY-01 (2026-08-07, slice 3) — the custody
    # detail template renders per-line attachments inline (one
    # documents_for call per settlement line). Passing it through
    # render_template would force every callsite of the polymorphic
    # Document flow to hand-plumb the same helper. Exposing as a
    # global keeps the pattern DRY.
    from app.services.opsflow_extras import documents_for as _docs_for
    app.jinja_env.globals["documents_for"] = _docs_for

    # MARSOUD-APPROVAL-GATED-SUPERADMIN (2026-08-12) — badge on
    # the /admin sidebar's "موافقات مُعلَّقة" nav row. Cheap
    # indexed COUNT queried once per request. Wrapped so
    # anonymous / non-superadmin renders never explode if the
    # migration hasn't run yet.
    def _pending_actions_nav_count():
        try:
            from app.services.superadmin_approval import pending_count
            return pending_count()
        except Exception:
            return 0
    app.jinja_env.globals["pending_actions_nav_count"] = (
        _pending_actions_nav_count)

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

    # MARSOUD-PLANS-COMPLETE (Abdelhamid 2026-07-22) — CLI:
    #   flask seed-plans    (idempotent renamer + quota-row seeder)
    from app.cli import register as _register_plans_cli
    _register_plans_cli(app)

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

    # MARSOUD-ROLE-SYNC (2026-08-03) — CLI:
    #   flask backfill-role-sync                 (dry-run)
    #   flask backfill-role-sync --apply         (write)
    # Repairs user_companies rows where the legacy `role` string drifted
    # from the role_id FK (old invitation-accept path).
    from scripts.backfill_role_sync import backfill_cli as _role_sync_cli
    app.cli.add_command(_role_sync_cli)

    # MARSOUD-OPS-FOUNDATION (2026-08-05) — CLI:
    #   flask backfill-ops-accounts              (dry-run)
    #   flask backfill-ops-accounts --apply      (write)
    # Adds 1170 + 5940 to companies created before those accounts existed.
    # seed_default_coa only runs at company creation, so without this the
    # new operations work for new tenants and fail for old ones.
    from scripts.backfill_ops_accounts import backfill_cli as _ops_acc_cli
    app.cli.add_command(_ops_acc_cli)

    # MARSOUD-METRIC-AUTOMATION (2026-08-05) — CLI:
    #   flask open-cycle-now                 (dry-run)
    #   flask open-cycle-now --apply         (write)
    # The ticket's one-off: August 2026's cycle starts on the day this
    # deploys, not the 1st, and no earlier data is backfilled. That date
    # is not knowable from the code, so it is a command run on the day.
    # From September the cron job handles it.
    from scripts.open_evaluation_cycle import backfill_cli as _open_cycle_cli
    app.cli.add_command(_open_cycle_cli)

    # MARSOUD-DOUBLE-REVERSAL-DIAG (2026-08-06) — CLI:
    #   flask audit-double-reversals                  # all companies
    #   flask audit-double-reversals --company-id 8   # one company
    # Read-only report on journal entries that carry MORE THAN ONE
    # active reversal — the aftermath of double-reverse bugs from
    # before the MARSOUD-REVERSE-ONCE guard. No --apply flag by
    # design; the ticket is explicit that repair is a separate
    # decision per case.
    from scripts.audit_double_reversals import audit_cli as _dblrev_cli
    app.cli.add_command(_dblrev_cli)

    # MARSOUD-COSMETICS-CATEGORY-SEED (2026-08-07) — CLI:
    #   flask seed-cosmetics-categories --company-id 106            # dry-run
    #   flask seed-cosmetics-categories --company-id 106 --apply    # write
    from scripts.seed_cosmetics_categories import seed_cli as _cosmetics_seed_cli
    app.cli.add_command(_cosmetics_seed_cli)

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

    # MARSOUD-SUPERADMIN-CONTROL-01 T1 (2026-08-08) — CLI:
    # flask check-registry
    @app.cli.command("check-registry")
    def _check_registry():
        """Report drift between the feature registry and the rest
        of the app. Exits nonzero on any finding — safe to wire
        into pre-deploy CI."""
        import sys
        from app.services.feature_registry import (
            all_modules, all_features,
            module_for_permission, all_module_codes,
        )
        findings = []

        # (a) plan seed lists a module the registry doesn't know.
        try:
            from app.cli import PLAN_SEED
            registry_codes = all_module_codes()
            for cfg in PLAN_SEED:
                for m in cfg["modules"]:
                    if m not in registry_codes:
                        findings.append(
                            f"PLAN_SEED[{cfg['code']!r}] lists module "
                            f"{m!r} that has no entry in feature_registry")
        except Exception as e:
            findings.append(f"PLAN_SEED check failed: {e}")

        # (b) DB-side FeatureFlag rows keyed by a module we don't
        # know. Skip cleanly if the table isn't there.
        try:
            from app.models import FeatureFlag
            for ff in FeatureFlag.query.all():
                if ff.module_key not in registry_codes:
                    findings.append(
                        f"FeatureFlag row {ff.module_key!r} points "
                        f"at an unknown module")
        except Exception:
            pass   # table missing → fresh install, safe to skip

        # (c) Every P permission code maps to some module.
        # MARSOUD-4-BRANCH-REPAIR (2026-08-08) — the 4 crm.* fine-
        # grained codes intentionally have no plan-level module
        # gate (see the "3. Every P permission maps..." check in
        # tests/audit_registry_and_access.py for the rationale).
        # Whitelist them so `flask check-registry` stays green.
        _UNGATED_AT_PLAN = {
            "crm.campaigns.view",
            "crm.activities.view",
            "crm.contacts.view",
            "crm.analytics.view",
        }
        try:
            from app.services.permissions import P
            missing_perm_module = []
            for perm in sorted(P.keys()):
                if module_for_permission(perm) is None:
                    # Filter out the `settings`-owned perms
                    # (users.view/manage) that don't have a
                    # prefix mapping because settings has no
                    # prefix — this is fine, they're always
                    # allowed. Only report unknown prefixes.
                    if (not perm.startswith(("users.",))
                            and perm not in _UNGATED_AT_PLAN):
                        missing_perm_module.append(perm)
            if missing_perm_module:
                findings.append(
                    f"{len(missing_perm_module)} permission code(s) "
                    f"in permissions.P have no module in the "
                    f"registry: {missing_perm_module[:5]}"
                    + (" …" if len(missing_perm_module) > 5 else ""))
        except Exception as e:
            findings.append(f"permissions.P check failed: {e}")

        # (d) Every module has at least one feature (helps catch a
        # module code introduced without wiring endpoints).
        try:
            feats_by_module = {}
            for f in all_features():
                feats_by_module.setdefault(f.module, []).append(f)
            for m in all_modules():
                if not feats_by_module.get(m.code):
                    findings.append(
                        f"module {m.code!r} has zero features "
                        f"tied to it")
        except Exception as e:
            findings.append(f"module→feature check failed: {e}")

        # Report
        # ASCII-only prints — Windows cp1252 stdout crashes on the
        # ✓ / ❌ glyphs, which the pre-commit hook triggers.
        #
        # MARSOUD-CLI-MERGE-REPAIR (2026-08-24) — this report block
        # was lost from here by the f5105ed "merge: T8 route-audit"
        # merge and landed at the TOP of `_audit_routes` below. That
        # broke both commands at once: `check-registry` computed
        # `findings` and then fell off the end of the function, so it
        # always exited 0 and printed nothing no matter how much
        # drift there was; and `audit-routes` raised NameError on
        # `findings` (a local of THIS function) before reaching a
        # single line of its own route-coverage logic. Restored here,
        # deleted there.
        def _p(msg):
            try:
                print(msg)
            except UnicodeEncodeError:
                print(msg.encode("ascii", errors="replace").decode())

        if not findings:
            _p("OK feature registry is in sync - no drift detected")
            _p(f"  - {len(list(all_modules()))} modules")
            _p(f"  - {len(list(all_features()))} features")
            sys.exit(0)
        _p("FAIL feature registry drift detected:")
        for finding in findings:
            _p(f"  - {finding}")
        sys.exit(1)

    # MARSOUD-SUPERADMIN-CONTROL-01 T8 (2026-08-08) — CLI:
    # flask audit-routes
    @app.cli.command("audit-routes")
    def _audit_routes():
        """Report GET pages with no template link. Exits nonzero
        on any orphan not classified in route_audit_ignore.txt.

        Wire into pre-deploy CI to prevent shipping a screen with
        no way to reach it."""
        import sys
        # ASCII-only prints — Windows cp1252 stdout crashes on
        # ✓ / ❌ glyphs when a pre-commit hook fires.
        def _p(msg):
            try:
                print(msg)
            except UnicodeEncodeError:
                print(msg.encode("ascii", errors="replace").decode())

        try:
            from app.services.route_audit import (
                build_coverage, orphans, stale_ignores, summary,
            )
        except Exception as e:
            _p(f"FAIL failed to import route_audit: {e}")
            sys.exit(2)
        # Ignore-file syntax validation happens on read; catch
        # bad-format lines up front with a clear message.
        try:
            rows = build_coverage(app)
        except ValueError as e:
            _p(f"FAIL route_audit_ignore.txt: {e}")
            sys.exit(1)
        stale = stale_ignores(app)
        if stale:
            _p(f"FAIL {len(stale)} stale ignore entries "
               "(endpoints no longer exist):")
            for ep in stale:
                _p(f"  - {ep}")
            sys.exit(1)
        orphs = orphans(app)
        s = summary(app)
        if orphs:
            _p(f"FAIL {len(orphs)} orphan endpoint(s) "
               "(HTML pages with no template link):")
            for r in orphs:
                _p(f"  - {r.endpoint}  ({r.url_rule})  "
                   f"module={r.module}")
            _p("")
            _p("Fix by adding a template link OR adding an entry "
               "to route_audit_ignore.txt with a reason.")
            sys.exit(1)
        _p(f"OK all {s['pages']} pages have a template link "
           "(or a classified ignore entry).")
        _p(f"  - {s['total']} total endpoints")
        _p(f"  - {s['pages']} pages ({s['ignored']} via ignore file)")
        _p(f"  - {s['api']} api / {s['post_only']} post-only / "
           f"{s['exempt']} exempt")
        sys.exit(0)

    return app
