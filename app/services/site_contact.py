"""MARSOUD-LANDING-RESTRICT-REGISTER-01 (2026-10-28) — single source
of truth for the public WhatsApp-signup CTA.

The ticket retires marsoud's public self-signup form and routes every
"ابدأ مجانًا" / "إنشاء حساب" button to a prefilled WhatsApp chat.
Keeping the phone number + Arabic prefill message in one module means
tomorrow's number change hits ONE line, not twenty templates.

Consumers:
  * `app/routes/auth.py:register()` — redirects `/register` here
    (backend-level block per the ticket's AC #3; a crafted POST never
    reaches the User-creation code).
  * `app/__init__.py` — registered as a Jinja global so any rendered
    template can `href="{{ whatsapp_signup_url() }}"`.
  * `app/static/landing.html` — served raw via send_from_directory
    (NOT rendered through Jinja), so it hard-codes the URL inline.
    The audit greps the static file for `WHATSAPP_SIGNUP_NUMBER` to
    catch drift between the helper and the baked-in URL.
"""
from urllib.parse import quote


WHATSAPP_SIGNUP_NUMBER = "201044203322"      # +20 104 420 3322 (Egypt)
WHATSAPP_SIGNUP_MESSAGE = (
    "مرحبًا، أنا مهتم بالتسجيل في نظام مرصود، "
    "وأرغب في معرفة التفاصيل وطريقة الاشتراك."
)


def whatsapp_signup_url() -> str:
    """Return the official wa.me URL with the ticket's Arabic prefill
    message percent-encoded.  Safe to use in server redirects, Jinja
    templates (via the registered global), and backend tests.
    """
    return (f"https://wa.me/{WHATSAPP_SIGNUP_NUMBER}"
            f"?text={quote(WHATSAPP_SIGNUP_MESSAGE)}")
