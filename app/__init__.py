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

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

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

    @app.before_request
    def load_active_company():
        from flask_login import current_user
        from flask import abort
        from app.services.superadmin import IMPERSONATION_SESSION_KEY
        g.active_company = None
        g.user_companies = []
        g.impersonating = False
        if current_user.is_authenticated:
            non_suspended = [c for c in current_user.companies
                             if (c.status or "ACTIVE") != "SUSPENDED"]
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

    @app.context_processor
    def inject_globals():
        from datetime import datetime
        from app.services.permissions import has_permission, get_user_role
        active_company = g.get("active_company")
        current_role = None
        if active_company:
            from flask_login import current_user as _cu
            if _cu.is_authenticated:
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
        return {
            "active_company": active_company,
            "user_companies": g.get("user_companies", []),
            "now": datetime.utcnow(),
            "has_permission": has_permission,
            "current_role": current_role,
            "impersonating": g.get("impersonating", False),
            "subscription": sub_state,
            "subitem_allowed": _subitem_allowed_template,
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
                return {"notif_unread_count": unread_count_for(current_user.id)}
        except Exception:
            pass
        return {"notif_unread_count": 0}

    @app.template_filter("money")
    def money_filter(value, currency=None):
        if value is None:
            return "0.00"
        try:
            return f"{float(value):,.2f}"
        except (TypeError, ValueError):
            return str(value)

    return app
