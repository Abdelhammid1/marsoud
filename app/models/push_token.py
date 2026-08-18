"""MARSOUD-MOBILE-TKT-05 (2026-08-18) — FCM push registration
tokens per (user, device). One row per active device — a user
can have multiple phones, and each phone rotates its FCM token
whenever the app data is cleared or the app is reinstalled.

The mobile client POSTs its current FCM token to
`POST /api/v1/my/push-tokens` on every successful login + on
every FirebaseMessaging.instance.onTokenRefresh callback. The
backend upserts by (user_id, token) so the row is idempotent.

`platform` is `"android" | "ios" | "web"` — used only for
diagnostics so a super-admin can see "user X has 2 Android + 1
iOS registered".

`is_active` flips to False when FCM returns
`UNREGISTERED` / `INVALID_ARGUMENT` for the token so
subsequent sends skip it. A cron sweep (optional, future) can
purge rows older than 90 days with is_active=False.
"""
from datetime import datetime
from app import db


class PushToken(db.Model):
    __tablename__ = "push_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer,
                         db.ForeignKey("users.id",
                                        ondelete="CASCADE"),
                         nullable=False, index=True)
    # FCM registration token — max ~300 chars in practice; we
    # allow 400 for safety.
    token = db.Column(db.String(400), nullable=False, index=True)
    platform = db.Column(db.String(16), nullable=False,
                          default="android")
    # Free-text device label the app sends (model name, OS
    # version). Useful for the super-admin's device list.
    device_label = db.Column(db.String(120), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False,
                           default=True, index=True)
    last_used_at = db.Column(db.DateTime, default=datetime.utcnow,
                              nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow,
                            nullable=False)

    user = db.relationship("User")

    __table_args__ = (
        db.UniqueConstraint("user_id", "token",
                             name="uq_push_tokens_user_token"),
    )

    def __repr__(self):
        state = "active" if self.is_active else "inactive"
        return (f"<PushToken user={self.user_id} "
                f"{self.platform} {state}>")
