import os
from pathlib import Path
from dotenv import load_dotenv

basedir = Path(__file__).parent.absolute()
load_dotenv(basedir / ".env")


class Config:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
    # MARSOUD-SAAS-SUBDOMAIN — share the session cookie across all
    # *.marsoud.com subdomains so switching companies (tenant subdomains)
    # doesn't force a re-login.
    #
    # MARSOUD-SESSION-COOKIE-DEV-FIX (Ibrahim 2026-07-18) — default is
    # None so localhost + test_client work out-of-the-box. Every audit
    # + Playwright suite was breaking with 302 → /login because a
    # cookie scoped to `.marsoud.com` isn't attached to `localhost`
    # requests. Production MUST override this in .env:
    #
    #     SESSION_COOKIE_DOMAIN=.marsoud.com
    #
    # Without that override in production, subdomain login isolation
    # would break (each subdomain would look like a separate session).
    SESSION_COOKIE_DOMAIN = os.environ.get("SESSION_COOKIE_DOMAIN") or None
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{basedir / 'instance' / 'ledgeros.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    DEFAULT_CURRENCY = "SAR"
    SUPPORTED_CURRENCIES = ["SAR", "EGP", "USD", "EUR", "AED"]
    DEFAULT_LOCALE = "ar"

    # SMTP — falls back to log-only mode if credentials missing
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", "no-reply@marsoud.app")
    SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "Marsoud")
    SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
    CRON_TOKEN = os.environ.get("CRON_TOKEN", "")

    # MARSOUD-PUBLIC-CONTACT-FORM-01 (Abdelhamid 2026-07-24) —
    # Manasty's own company id in this deployment. Every ticket that
    # writes into Manasty (public contact form → Lead, support
    # tickets cross-tenant permission) reads this. Env var so an
    # accidental DB id swap doesn't need a code push.
    MANASTY_COMPANY_ID = int(os.environ.get("MANASTY_COMPANY_ID", 8))

    # Public /api/v1/public/contact-lead token. If empty, the
    # endpoint refuses EVERY request (fail-closed) — same lesson we
    # learned the hard way with CRON_TOKEN.
    CONTACT_FORM_TOKEN = os.environ.get("CONTACT_FORM_TOKEN", "")
