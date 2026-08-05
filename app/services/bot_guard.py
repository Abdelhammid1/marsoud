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

  5. Math challenge — MARSOUD-ATTENDANCE-ANTIBOT (2026-08-05). A
     two-number sum or difference the user answers before an
     attendance check-in is accepted. Deliberately arithmetic and
     not visual: image captchas were ruled out for this product.
     Lives here rather than in a new module because this file is
     already the one place bot defences are described.

All three checks run BEFORE any DB write. `honeypot_tripped()`
returns True silently so the caller can early-return a 200 without
inserting anything.
"""
import random
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


# ─── 4. Cloudflare Turnstile ─────────────────────────────────────
# Server-side CAPTCHA verification. Turnstile is enabled only when
# BOTH TURNSTILE_SITE_KEY (front-end widget) and
# TURNSTILE_SECRET_KEY (server-side verify call) are configured.
# When either is empty, is_turnstile_enabled() returns False and
# verify_turnstile() short-circuits to True — honeypot + rate limit
# still enforce protection.
TURNSTILE_VERIFY_URL = ("https://challenges.cloudflare.com/turnstile/v0/"
                         "siteverify")


def is_turnstile_enabled():
    """True when both keys are configured."""
    from flask import current_app
    return bool(
        current_app.config.get("TURNSTILE_SITE_KEY") and
        current_app.config.get("TURNSTILE_SECRET_KEY")
    )


def verify_turnstile(token, remote_ip=None, timeout_secs=5):
    """POST the widget's response token to Cloudflare and return
    True/False. When Turnstile isn't configured, returns True (no
    challenge means no failure). Never raises — network errors +
    Cloudflare 5xx return False so the caller can 403 the request."""
    from flask import current_app
    if not is_turnstile_enabled():
        return True
    if not token:
        return False
    secret = current_app.config.get("TURNSTILE_SECRET_KEY")
    import urllib.parse
    import urllib.request
    payload = {"secret": secret, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        TURNSTILE_VERIFY_URL, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
            import json as _json
            body = _json.loads(resp.read().decode("utf-8"))
        return bool(body.get("success"))
    except Exception:
        current_app.logger.exception("Turnstile verify failed")
        return False


# ─── 5. Client IP resolver ───────────────────────────────────────
def client_ip(request):
    """Pick a stable IP identifier for rate-limit bucketing. Behind
    nginx we trust the first entry in X-Forwarded-For; falls back
    to REMOTE_ADDR for local dev + direct-access tests."""
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "?"


# ─── 5. Math challenge (MARSOUD-ATTENDANCE-ANTIBOT, 2026-08-05) ──
# The answer lives in the session, never in the form, so a bot
# cannot read it off the page it is posting. It is CONSUMED on the
# first verification: a correct answer works exactly once, which is
# what stops a script solving one challenge by hand and then
# replaying that same answer twice a day forever.
_MATH_SESSION_KEY = "attendance_math_answer"

# Small numbers on purpose. This is a liveness check, not a test —
# an employee stabbing at a phone before their shift should clear it
# in one attempt.
_MATH_MAX = 9


def generate_math_challenge():
    """Return (question_text, ) and stash the answer in the session.

    Subtraction is ordered so the answer is never negative — an
    employee typing "-3" because the UI asked for 2-5 would be a
    failure of the challenge, not of the person.
    """
    from flask import session
    a = random.randint(1, _MATH_MAX)
    b = random.randint(1, _MATH_MAX)
    if random.choice((True, False)):
        question, answer = f"{a} + {b}", a + b
    else:
        hi, lo = max(a, b), min(a, b)
        question, answer = f"{hi} - {lo}", hi - lo
    session[_MATH_SESSION_KEY] = answer
    return question


def verify_math_challenge(submitted):
    """True when `submitted` matches the pending challenge.

    Consumes the challenge either way. A wrong answer therefore
    forces a fresh question rather than letting a script brute-force
    the same one — there are only 19 possible answers, so a reusable
    challenge would be worth nothing.
    """
    from flask import session
    expected = session.pop(_MATH_SESSION_KEY, None)
    if expected is None:
        return False
    try:
        return int(str(submitted).strip()) == int(expected)
    except (TypeError, ValueError):
        return False


def math_challenge_pending():
    """Whether a challenge is currently outstanding. For templates that
    need to know whether to re-render the question."""
    from flask import session
    return _MATH_SESSION_KEY in session
