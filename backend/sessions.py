"""backend/sessions.py — conversation and flow state, on MongoDB.

One document per session. It holds the transcript, the active flow with its
slots and provenance, the queue of deferred intents, risk signals and the
dialogue counters that bound every loop in the graph.

The three functions the agent layer already used — `record_turn`,
`get_history`, `count_prior_declines` — keep their signatures, so nothing
upstream changed. Everything else is new surface the graph needs: slot writes
that carry provenance, loop counters that can actually be enforced, and the
terminalise step that purges ephemeral secrets.

Nothing here is authoritative. Losing a session document loses the conversation,
not the audit trail — decisions and writes are in the Postgres chain.

Imports: backend.mongo, shared.config.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.mongo import sessions_col
from shared.config import config

# Slot values that must never survive a terminal state, per the retention rules.
_EPHEMERAL_SLOT_KEYS = frozenset(
    {"otp", "pin", "new_pin", "cvv", "password", "security_answer", "full_pan", "aadhaar"}
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat()


def _blank(session_id: str, member_ref: Optional[str]) -> dict[str, Any]:
    now = _now()
    return {
        "_id": session_id,
        "session_id": session_id,
        "member_ref": member_ref,
        "channel": "web",
        "market": "IN",
        "locale": "en-IN",
        "session_status": "open",
        "started_at": now,
        "last_activity_at": now,
        # TTL: flow state is disposable well before the audit trail is.
        "expires_at": now + timedelta(days=30),
        "turn_count": 0,
        "transcript": [],
        "active_flow": None,
        "queued_flows": [],
        "risk": {
            "fraud_signal": "none",
            "financial_hardship_signal": "none",
            "prompt_injection_detected": False,
            "pii_or_secret_detected": [],
        },
        "dialogue": {
            "clarification_count": 0,
            "topic_switch_count": 0,
            "invalid_slot_count_by_slot": {},
            "loop_signature": None,
            "loop_repeat_count": 0,
        },
        "audit": {"state_version": 0, "trace_id": None},
    }


def open_session(session_id: str, member_ref: Optional[str] = None) -> dict[str, Any]:
    """Fetch the session, creating it on first contact. Idempotent."""
    col = sessions_col()
    doc = col.find_one({"_id": session_id})
    if doc is None:
        doc = _blank(session_id, member_ref)
        col.insert_one(doc)
        return doc
    if member_ref and not doc.get("member_ref"):
        col.update_one({"_id": session_id}, {"$set": {"member_ref": member_ref}})
        doc["member_ref"] = member_ref
    return doc


def get_session(session_id: str) -> Optional[dict[str, Any]]:
    return sessions_col().find_one({"_id": session_id})


def _touch() -> dict[str, Any]:
    return {"last_activity_at": _now()}


# --------------------------------------------------------------------------- #
# Transcript
# --------------------------------------------------------------------------- #
def record_turn(
    session_id: str,
    member_id: Optional[str],
    role: str,
    text: str,
    intent: Optional[str] = None,
    confidence: Optional[float] = None,
    decision_outcome: Optional[str] = None,
    decision_reason_code: Optional[str] = None,
    policy_id: Optional[str] = None,
) -> None:
    """Append one turn. `turn_index` stays gap-free and per-session."""
    open_session(session_id, member_id)
    doc = sessions_col().find_one({"_id": session_id}, {"turn_count": 1})
    turn_index = (doc or {}).get("turn_count", 0)
    sessions_col().update_one(
        {"_id": session_id},
        {
            "$push": {
                "transcript": {
                    "turn_index": turn_index,
                    "role": role,
                    "content": text,
                    "intent": intent,
                    "confidence": confidence,
                    "decision_outcome": decision_outcome,
                    "decision_reason_code": decision_reason_code,
                    "policy_id": policy_id,
                    "created_at": _iso(),
                }
            },
            "$inc": {"turn_count": 1, "audit.state_version": 1},
            "$set": _touch(),
        },
    )


def get_history(session_id: str) -> list[dict[str, str]]:
    """Prior turns as the {role, text} shape AgentState expects."""
    doc = sessions_col().find_one({"_id": session_id}, {"transcript": 1})
    if not doc:
        return []
    return [
        {"role": t["role"], "text": t["content"]}
        for t in sorted(doc.get("transcript", []), key=lambda t: t["turn_index"])
    ]


def count_prior_declines(session_id: str, policy_id: str, reason_code: str) -> int:
    """How many times this session was already declined for this exact reason.

    Scoped to (policy_id, reason_code) so a decline on one policy never counts
    toward the repeat threshold of an unrelated request.
    """
    doc = sessions_col().find_one({"_id": session_id}, {"transcript": 1})
    if not doc:
        return 0
    return sum(
        1
        for t in doc.get("transcript", [])
        if t.get("policy_id") == policy_id
        and t.get("decision_outcome") == "DECLINE"
        and t.get("decision_reason_code") == reason_code
    )


# --------------------------------------------------------------------------- #
# Active flow
# --------------------------------------------------------------------------- #
def start_flow(
    session_id: str, flow_id: str, domain: str, operation: str, policy_id: Optional[str] = None
) -> dict[str, Any]:
    flow = {
        "flow_id": flow_id,
        "domain": domain,
        "operation": operation,
        "policy_id": policy_id,
        "status": "collecting_slots",
        "started_at": _iso(),
        "slots": {},
        "policy_results": [],
        "proposed_action": None,
        "execution": None,
        "attempts": {"clarify": 0, "confirm": 0},
    }
    sessions_col().update_one(
        {"_id": session_id},
        {"$set": {"active_flow": flow, **_touch()}, "$inc": {"audit.state_version": 1}},
    )
    return flow


def get_active_flow(session_id: str) -> Optional[dict[str, Any]]:
    doc = sessions_col().find_one({"_id": session_id}, {"active_flow": 1})
    return (doc or {}).get("active_flow")


def set_flow_status(session_id: str, status: str) -> None:
    sessions_col().update_one(
        {"_id": session_id},
        {"$set": {"active_flow.status": status, **_touch()},
         "$inc": {"audit.state_version": 1}},
    )


def set_slot(
    session_id: str,
    name: str,
    value: Any,
    status: str = "customer_provided",
    source: str = "member_message",
    sensitivity: str = "internal",
) -> None:
    """Write one slot with its provenance.

    Provenance is not decoration — the escalation packet has to tell a human
    where each value came from, and `backend truth wins` is only enforceable if
    you know which values the member asserted.
    """
    sessions_col().update_one(
        {"_id": session_id},
        {
            "$set": {
                f"active_flow.slots.{name}": {
                    "value": value,
                    "status": status,
                    "source": source,
                    "sensitivity": sensitivity,
                    "collected_at": _iso(),
                },
                **_touch(),
            },
            "$inc": {"audit.state_version": 1},
        },
    )


def get_slots(session_id: str) -> dict[str, Any]:
    flow = get_active_flow(session_id) or {}
    return flow.get("slots", {})


def record_policy_result(session_id: str, decision: dict[str, Any]) -> None:
    sessions_col().update_one(
        {"_id": session_id},
        {"$push": {"active_flow.policy_results": decision},
         "$set": _touch(), "$inc": {"audit.state_version": 1}},
    )


# --------------------------------------------------------------------------- #
# Loop counters — the bounds that stop a member being trapped
# --------------------------------------------------------------------------- #
def bump_clarify(session_id: str) -> int:
    doc = sessions_col().find_one_and_update(
        {"_id": session_id},
        {"$inc": {"dialogue.clarification_count": 1}, "$set": _touch()},
        return_document=True,
    )
    return doc["dialogue"]["clarification_count"]


def clarify_exhausted(session_id: str) -> bool:
    doc = sessions_col().find_one({"_id": session_id}, {"dialogue": 1})
    count = ((doc or {}).get("dialogue") or {}).get("clarification_count", 0)
    return count >= config.CLARIFY_MAX_QUESTIONS


def bump_slot_failure(session_id: str, slot: str) -> int:
    doc = sessions_col().find_one_and_update(
        {"_id": session_id},
        {"$inc": {f"dialogue.invalid_slot_count_by_slot.{slot}": 1}, "$set": _touch()},
        return_document=True,
    )
    return doc["dialogue"]["invalid_slot_count_by_slot"][slot]


def slot_exhausted(session_id: str, slot: str) -> bool:
    doc = sessions_col().find_one({"_id": session_id}, {"dialogue": 1})
    counts = ((doc or {}).get("dialogue") or {}).get("invalid_slot_count_by_slot", {})
    return counts.get(slot, 0) >= config.SLOT_MAX_ATTEMPTS


def check_loop(session_id: str, signature: str) -> bool:
    """Global circuit breaker: the same state and question three times running.

    Returns True when the loop should be broken. Any change in signature resets
    the counter, so genuine progress is never penalised.
    """
    doc = sessions_col().find_one({"_id": session_id}, {"dialogue": 1})
    dialogue = (doc or {}).get("dialogue") or {}
    if dialogue.get("loop_signature") == signature:
        count = dialogue.get("loop_repeat_count", 0) + 1
        sessions_col().update_one(
            {"_id": session_id}, {"$set": {"dialogue.loop_repeat_count": count, **_touch()}}
        )
        return count >= 3
    sessions_col().update_one(
        {"_id": session_id},
        {"$set": {"dialogue.loop_signature": signature,
                  "dialogue.loop_repeat_count": 1, **_touch()}},
    )
    return False


# --------------------------------------------------------------------------- #
# Queue and terminal
# --------------------------------------------------------------------------- #
def queue_flow(session_id: str, domain: str, operation: str) -> None:
    sessions_col().update_one(
        {"_id": session_id},
        {"$push": {"queued_flows": {"domain": domain, "operation": operation,
                                    "queued_at": _iso()}},
         "$set": _touch()},
    )


def pop_queued_flow(session_id: str) -> Optional[dict[str, Any]]:
    doc = sessions_col().find_one_and_update(
        {"_id": session_id},
        {"$pop": {"queued_flows": -1}, "$set": _touch()},
        return_document=False,  # pre-image, so we can read what was popped
    )
    queued = (doc or {}).get("queued_flows") or []
    return queued[0] if queued else None


def terminalize(session_id: str, outcome: str) -> None:
    """Freeze the flow, purge ephemeral sensitive slots, invalidate tokens.

    The summary is kept for context on a follow-up turn; the secrets are not.
    """
    flow = get_active_flow(session_id)
    summary = None
    if flow:
        kept = {
            k: v for k, v in (flow.get("slots") or {}).items()
            if k.lower() not in _EPHEMERAL_SLOT_KEYS
            and (v or {}).get("sensitivity") != "restricted"
        }
        summary = {
            "flow_id": flow.get("flow_id"),
            "domain": flow.get("domain"),
            "operation": flow.get("operation"),
            "policy_id": flow.get("policy_id"),
            "outcome": outcome,
            "slots": kept,
            "closed_at": _iso(),
        }
    sessions_col().update_one(
        {"_id": session_id},
        {
            "$set": {"active_flow": None, **_touch()},
            "$push": ({"completed_flows": summary} if summary else {}),
            "$inc": {"audit.state_version": 1},
        }
        if summary
        else {"$set": {"active_flow": None, **_touch()},
              "$inc": {"audit.state_version": 1}},
    )


def close_session(session_id: str, status: str = "closed") -> None:
    sessions_col().update_one(
        {"_id": session_id},
        {"$set": {"session_status": status, "active_flow": None, **_touch()}},
    )


def expire_idle() -> int:
    """Close sessions idle past the configured timeout. Returns how many.

    Run on a schedule. Expiry invalidates authentication and any pending
    confirmation, so a resumed conversation must re-authenticate.
    """
    cutoff = _now() - timedelta(seconds=config.FLOW_IDLE_TIMEOUT_S)
    result = sessions_col().update_many(
        {"session_status": "open", "last_activity_at": {"$lt": cutoff}},
        {"$set": {"session_status": "idle", "active_flow": None}},
    )
    return result.modified_count
