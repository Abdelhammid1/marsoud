"""MARSOUD-BOT-PROTECTION-01 (Abdelhamid 2026-07-24).

Multi-layer defense against automated fake company signups:

  1. Honeypot field — an invisible <input name="website"> that
     humans never see (CSS display:none). Bots that iterate over
     every form field WILL fill it, so any non-empty submission is
     a bot. We answer with a soft-success (200) but do NOTHING
     to make the bot think it succeeded → detection stays hidden.

  2. Rate limit — per-IP sliding window, 5 attempts / hour on
     /register. Bots pumping thousands of variations from a single
     Tor exit stall out fast.

  3. Spam-domain filter — reject known disposable email providers
     at submission time. List is intentionally short + editable in
     one place so real dev shops can grow it.

  4. (Not here — separate module) Cloudflare Turnstile widget on
     the register form validated server-side.

All three checks run BEFORE any DB write. `honeypot_tripped()`
returns True silently so the caller can early-return a 200 without
inserting anything.
"""
import re
import threading
import time
from collections import deque


# ─── 1. Honeypot ─────────────────────────────────────────────────
# CSS-hidden input name — must match the hidden field in the
# register template exactly. "website" is a plausible-looking name
# that bots typically fill; if you rename it, rename the template.
HONEYPOT_FIELD = "website"


def honeypot_tripped(form):
    """Returns True when the honeypot field is non-empty. Callers
    should soft-succeed to avoid revealing the trap."""
    val = (form.get(HONEYPOT_FIELD) or "").strip()
    return bool(val)


# ─── 2. Rate limit (per-IP sliding window, in-memory) ────────────
_REGISTER_WINDOW_SECS = 3600   # 1 hour
_REGISTER_MAX_PER_WINDOW = 5   # per the ticket
_ip_history = {}               # ip → deque[timestamps]
_lock = threading.Lock()


def register_rate_ok(ip):
    """Returns True if this IP is within the allowed rate. False =
    caller should return 429."""
    now = time.monotonic()
    with _lock:
        q = _ip_history.setdefault(ip, deque())
        while q and (now - q[0]) > _REGISTER_WINDOW_SECS:
            q.popleft()
        if len(q) >= _REGISTER_MAX_PER_WINDOW:
            return False
        q.append(now)
        return True


def register_rate_reset(ip=None):
    """Test-only helper. In production the window expires on its
    own via the deque-trim above."""
    with _lock:
        if ip is None:
            _ip_history.clear()
        else:
            _ip_history.pop(ip, None)


# ─── 3. Spam-domain filter ───────────────────────────────────────
# Known throwaway / disposable providers seen in the wild. Match
# is case-insensitive + covers subdomains (spam@x.mailinator.com
# also blocked). Editable in one place; extend as new providers
# show up.
SPAM_EMAIL_DOMAINS = frozenset({
    "mailinator.com", "tempmail.com", "temp-mail.org", "10minutemail.com",
    "10minutemail.net", "guerrillamail.com", "guerrillamail.net",
    "throwawaymail.com", "yopmail.com", "trashmail.com", "dispostable.com",
    "getnada.com", "sharklasers.com", "maildrop.cc", "fakeinbox.com",
    "mintemail.com", "mytemp.email", "spam4.me", "temporarymail.com",
    "trash-mail.com", "byebyemail.com", "hidemail.de", "spambog.com",
    "spamgourmet.com", "mvrht.net", "mailnesia.com", "mohmal.com",
    "emailondeck.com", "harakirimail.com", "moakt.com", "mvrht.com",
    "guerrillamail.info", "tempinbox.com", "instaddr.com",
})


_EMAIL_RE = re.compile(r"^[^\s@]+@([^\s@]+)$")


def is_spam_email(email):
    """True if the email is on the disposable-provider list."""
    if not email:
        return False
    m = _EMAIL_RE.match(email.strip().lower())
    if not m:
        # Malformed emails aren't spam per se — leave that check to
        # the caller's format validator.
        return False
    domain = m.group(1)
    # Direct match OR any parent domain (block subdomains too).
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in SPAM_EMAIL_DOMAINS:
            return True
    return False


# ─── 4. Client IP resolver ───────────────────────────────────────
def client_ip(request):
    """Pick a stable IP identifier for rate-limit bucketing. Behind
    nginx we trust the first entry in X-Forwarded-For; falls back
    to REMOTE_ADDR for local dev + direct-access tests."""
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "?"
