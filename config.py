import os
from pathlib import Path
from dotenv import load_dotenv

basedir = Path(__file__).parent.absolute()
load_dotenv(basedir / ".env")


class Config:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
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
