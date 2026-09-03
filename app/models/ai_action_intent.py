"""MARSOUD-AI-ACTION-FRAMEWORK-01 (2026-09-03) — Confirm-to-Execute
foundation.

`AiActionIntent` is the middle layer between the AI Accountant chat
and any write path in the app. The agent NEVER writes directly:

  1. Agent builds a payload → POST /api/ai-actions/propose → the
     row lands here in PENDING with a 15-minute expiry.
  2. Human clicks "تأكيد وتنفيذ" in the chat → POST
     /api/ai-actions/<id>/confirm → the service re-validates the
     payload against server-side sources (via the action's
     `fingerprint`) and, if nothing drifted, runs the SAME service
     function the visible UI uses. No duplicate write logic.

Distinct from `AgentProposal` (agent_proposal.py) which serves the
LLM's in-process tool loop with a 24h window and no server-side
freshness recheck. The two tables coexist by design — different
lifecycles, different callers.
"""
import enum
from datetime import datetime, timedelta
from app import db


# Ticket says six statuses. CONFIRMED is transient — set the moment
# the user clicks confirm; flips to EXECUTED / REJECTED / STALE
# depending on what the fingerprint check + executor say.
class AiActionIntentStatus(enum.Enum):
    PENDING   = "PENDING"     # created, waiting for user
    CONFIRMED = "CONFIRMED"   # user clicked; executor not done
    EXECUTED  = "EXECUTED"    # side-effect committed
    REJECTED  = "REJECTED"    # executor refused or user cancelled
    EXPIRED   = "EXPIRED"     # >15 min elapsed
    STALE     = "STALE"       # server-side data changed since propose

    @property
    def label_ar(self):
        return {
            "PENDING":   "بانتظار التأكيد",
            "CONFIRMED": "قيد التنفيذ",
            "EXECUTED":  "منفّذ",
            "REJECTED":  "مرفوض",
            "EXPIRED":   "انتهت الصلاحية",
            "STALE":     "البيانات تغيّرت — لازم اقتراح جديد",
        }.get(self.value, self.value)


# 15-minute default expiry per the ticket. Constant is module-level
# so the service and the model agree.
DEFAULT_EXPIRY_MINUTES = 15


class AiActionIntent(db.Model):
    __tablename__ = "ai_action_intents"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer,
                           db.ForeignKey("companies.id"),
                           nullable=False, index=True)

    # The action_type is a free string here (not an enum) so a new
    # Phase-1/2/3 ticket can add its own action without a migration.
    # Validation of "known vs unknown" is a service-layer concern
    # (_ACTION_REGISTRY membership check), not a DB constraint.
    action_type = db.Column(db.String(60), nullable=False, index=True)

    # Canonicalised JSON — dict keys sorted so
    # `{"a":1,"b":2}` and `{"b":2,"a":1}` compare equal.
    payload = db.Column(db.Text, nullable=False)
    # SHA-256 hex digest of the fingerprint computed at PROPOSE
    # time. At CONFIRM time the service recomputes the fingerprint
    # (which may consult server-side state) and refuses on mismatch.
    payload_hash = db.Column(db.String(64), nullable=False)

    status = db.Column(db.Enum(AiActionIntentStatus),
                       default=AiActionIntentStatus.PENDING,
                       nullable=False, index=True)

    # Who PROPOSED — always "ai_agent" for now, but leaving the
    # column open in case a human-typed autopilot ever needs to
    # route through the same pipeline. `proposed_by_user_id` is
    # the human whose chat session generated it — used for audit
    # and per-user rate-limiting in a future ticket.
    proposed_by = db.Column(db.String(60), nullable=False,
                             default="ai_agent")
    proposed_by_user_id = db.Column(db.Integer,
                                     db.ForeignKey("users.id"),
                                     nullable=True, index=True)

    # Who confirmed. Distinct from proposed_by_user_id because the
    # ticket says "any authenticated user in the tenant can confirm"
    # — an accountant can confirm a bill an owner's chat proposed.
    confirmed_by_user_id = db.Column(db.Integer,
                                      db.ForeignKey("users.id"),
                                      nullable=True)

    created_at = db.Column(db.DateTime,
                            default=datetime.utcnow, nullable=False)
    confirmed_at = db.Column(db.DateTime, nullable=True)
    executed_at = db.Column(db.DateTime, nullable=True)

    # 15 min after created_at at insert time. Any confirm attempt
    # after this refuses with 410 GONE and the row flips to EXPIRED.
    expires_at = db.Column(db.DateTime, nullable=False)

    # Executor's return value (json.dumps). Nullable because
    # PENDING / REJECTED / STALE rows never ran the executor.
    result_json = db.Column(db.Text, nullable=True)
    # Explanation for REJECTED / STALE — surfaces in the chat.
    reject_reason = db.Column(db.Text, nullable=True)

    company = db.relationship("Company")
    proposed_by_user = db.relationship(
        "User", foreign_keys=[proposed_by_user_id])
    confirmed_by_user = db.relationship(
        "User", foreign_keys=[confirmed_by_user_id])

    @property
    def is_open(self):
        """PENDING or CONFIRMED — neither an outcome yet."""
        return self.status in (AiActionIntentStatus.PENDING,
                                AiActionIntentStatus.CONFIRMED)

    def __repr__(self):
        return (f"<AiActionIntent {self.id} {self.action_type} "
                f"{self.status.value}>")
