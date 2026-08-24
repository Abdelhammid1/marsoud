"""MARSOUD-MOBILE-TKT-05 (2026-08-18) — Firebase Cloud Messaging
sender.

Wraps the `firebase-admin` SDK. One call site: `push_to_user(
user_id, title, body, ...)` — called from
`opsflow_extras.notify()` after the DB row is written.

Design invariants:

  1. **Silent when misconfigured.** If the service-account JSON
     isn't at the expected path (dev / test environments), every
     call is a no-op that logs at DEBUG level. Never raises. The
     in-app notification (row in `notifications` table) is
     always written; push is best-effort on top of that.

  2. **One initialization per process.** `firebase_admin.
     initialize_app()` complains if called twice. We track the
     init state on the module.

  3. **Automatic token pruning.** When FCM returns
     `UNREGISTERED` / `INVALID_ARGUMENT` for a token, we flip
     the PushToken row's `is_active=False`. Subsequent sends
     skip it. Prevents dead tokens from silently accumulating.

Config precedence (first match wins):
  · env var `FIREBASE_SERVICE_ACCOUNT_JSON` — the whole JSON
    contents inline (best for Heroku / Fly / Docker).
  · env var `FIREBASE_SERVICE_ACCOUNT_PATH` — filesystem path.
  · default path `<instance>/firebase-service-account.json` —
    the recommended dev setup.
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from flask import current_app

from app import db


_logger = logging.getLogger("marsoud.fcm")
_INIT_STATE = {"ready": False, "app": None, "attempted": False}


def _resolve_credentials():
    """Return a (credentials, project_id) tuple, or (None, None)
    when FCM is not configured for this environment."""
    inline = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if inline:
        try:
            data = json.loads(inline)
        except (TypeError, ValueError):
            _logger.warning(
                "FIREBASE_SERVICE_ACCOUNT_JSON is set but not "
                "valid JSON — falling back to file path lookup")
            data = None
        if data:
            from firebase_admin import credentials as _c
            return _c.Certificate(data), data.get("project_id")

    # File path.
    path_env = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH")
    candidate_paths = []
    if path_env:
        candidate_paths.append(Path(path_env))
    try:
        candidate_paths.append(
            Path(current_app.instance_path)
            / "firebase-service-account.json")
    except RuntimeError:
        # Called outside app context — skip default lookup.
        pass

    for p in candidate_paths:
        if p and p.is_file():
            try:
                with p.open("r", encoding="utf-8") as fh:
                    data = json.load(fh)
                from firebase_admin import credentials as _c
                return _c.Certificate(str(p)), data.get("project_id")
            except Exception:
                _logger.exception(
                    "failed reading firebase creds from %s", p)
                continue
    return None, None


def _ensure_initialized():
    """Lazy-init on first send. Returns True when FCM is usable
    for this call, False otherwise (misconfigured or the
    firebase_admin package isn't installed).

    Re-tries initialization if a previous attempt failed AND at
    least 5 minutes have passed — lets an admin drop the config
    file into place on a running server without a restart."""
    if _INIT_STATE["ready"]:
        return True
    try:
        import firebase_admin
    except ImportError:
        # firebase-admin not installed in this env (e.g. slim
        # test container). Push is silently disabled.
        if not _INIT_STATE.get("logged_missing"):
            _logger.info(
                "firebase-admin not installed — push disabled")
            _INIT_STATE["logged_missing"] = True
        return False

    creds, project_id = _resolve_credentials()
    if creds is None:
        # No credentials file. Common in dev/test — log once,
        # stay silent afterwards.
        if not _INIT_STATE.get("logged_no_creds"):
            _logger.info(
                "No Firebase service-account JSON found — push "
                "notifications disabled. Set "
                "FIREBASE_SERVICE_ACCOUNT_PATH or drop the JSON "
                "at instance/firebase-service-account.json to "
                "enable.")
            _INIT_STATE["logged_no_creds"] = True
        return False

    try:
        # Named app to avoid clashing with any other
        # firebase_admin initializations elsewhere in the
        # process. Idempotent — get_app raises ValueError when
        # the app doesn't exist; we catch and initialize.
        try:
            app = firebase_admin.get_app("marsoud")
        except ValueError:
            init_options = ({"projectId": project_id}
                             if project_id else {})
            app = firebase_admin.initialize_app(
                creds, init_options, name="marsoud")
        _INIT_STATE["ready"] = True
        _INIT_STATE["app"] = app
        _logger.info(
            "Firebase Admin ready (project=%s)", project_id)
        return True
    except Exception:
        _logger.exception("Firebase Admin init failed")
        return False


def is_configured():
    """External check for /admin diagnostics — does not raise."""
    return _ensure_initialized()


def _active_tokens_for(user_id):
    """Fetch all active PushToken rows for a user."""
    from app.models import PushToken
    return PushToken.query.filter_by(
        user_id=user_id, is_active=True).all()


def _mark_token_dead(token_str):
    """Called when FCM rejects a token as unregistered."""
    from app.models import PushToken
    rows = PushToken.query.filter_by(token=token_str).all()
    for r in rows:
        r.is_active = False
    if rows:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


def push_to_user(user_id, *, title, body=None, link_url=None,
                  kind=None, data_extras=None):
    """Send an FCM push to every active device of `user_id`.

    Best-effort. Never raises — always returns the count of
    successful sends. Callers pass the same title/body/link_url
    they wrote to the notifications table.

    `data_extras` is merged into the FCM `data` payload for
    client-side deep-linking (matches the notification kind so
    the app can route to the right screen).
    """
    if not _ensure_initialized():
        return 0
    tokens = _active_tokens_for(user_id)
    if not tokens:
        return 0
    try:
        from firebase_admin import messaging
    except ImportError:
        return 0

    payload_data = {"kind": str(kind or "")}
    if link_url:
        payload_data["link_url"] = link_url
    if data_extras:
        for k, v in data_extras.items():
            payload_data[str(k)] = str(v) if v is not None else ""

    sent = 0
    app_ref = _INIT_STATE.get("app")
    for tok in tokens:
        try:
            msg = messaging.Message(
                token=tok.token,
                notification=messaging.Notification(
                    title=title or "",
                    body=body or "",
                ),
                data=payload_data,
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(
                        sound="default",
                    ),
                ),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(
                            sound="default",
                            content_available=True,
                        ),
                    ),
                ),
            )
            messaging.send(msg, app=app_ref)
            tok.last_used_at = datetime.utcnow()
            sent += 1
        except Exception as e:
            # firebase_admin.messaging.UnregisteredError,
            # InvalidArgumentError → mark this token dead so we
            # skip it next round. Anything else, log + continue.
            err_name = type(e).__name__
            if err_name in ("UnregisteredError",
                             "InvalidArgumentError",
                             "SenderIdMismatchError"):
                _mark_token_dead(tok.token)
            else:
                _logger.warning(
                    "FCM send to %s failed (%s): %s",
                    user_id, err_name, e)
    if sent:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    return sent
