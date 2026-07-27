"""backend/sessions.py — conversation and flow state.

One document per session: the transcript, the active flow with its slots and
their provenance, the queue of deferred intents, risk signals, and the dialogue
counters that bound every loop in the graph.

The three functions the agent layer already used — `record_turn`,
`get_history`, `count_prior_declines` — keep their signatures. Everything else
is new surface the graph needs.

Storage is whole-document via `docstore`, so this file has one implementation
regardless of whether Mongo is running.

Nothing here is authoritative. Losing a session loses the conversation, not the
audit trail — decisions and writes live in the Postgres chain.

Imports: backend.docstore, shared.config.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend import docstore
from shared.config import config
from shared.redaction import redact

# Sensitivity is a property of WHAT the data is, not where it came from — a
# member typing a card nickname is `internal`, a member typing an OTP is
# `restricted`, same source. So it is looked up by slot name, never passed in
# by the caller and never inferred from `source`.
#
# An unlisted slot is `restricted`, not `internal`. Deliberate: the cost of the
# strict default is losing context on close, the cost of a permissive default is
# retaining a secret you promised not to. Only one of those is acceptable.
_SLOT_SENSITIVITY: dict[str, str] = {
    "otp": "restricted", "pin": "restricted", "new_pin": "restricted",
    "current_pin": "restricted", "cvv": "restricted", "cvv_or_cid": "restricted",
    "password": "restricted", "security_answer": "restricted",
    "full_pan": "restricted", "aadhaar": "restricted", "full_aadhaar": "restricted",
    "bank_account_number": "restricted",

    "member_reason": "confidential", "hardship_detail": "confidential",
    "dispute_reason": "confidential",

    "card_selection": "internal", "card_id": "internal",
    "fee_transaction_selection": "internal", "transaction_selection": "internal",
    "txn_ref": "internal", "fee_amount": "internal",
    "requested_limit": "internal", "new_limit": "internal",
    "requested_increase_pct": "internal", "replacement_reason": "internal",
    "reason": "internal", "delivery_address_choice": "internal",
    "statement_period": "internal", "new_address": "internal",
    # The graph's slot is named `address` (agent/nodes.py MEMBER_SLOTS,
    # agent/tools.py update_address). Only `new_address` was classified here,
    # so `address` fell to the restricted default and could never be collected
    # or retained. Same field, both spellings, same classification.
    "address": "internal",

    "information_topic": "public",
}
_DEFAULT_SENSITIVITY = "restricted"


def sensitivity_of(slot_name: str) -> str:
    """Classification for a slot. Unknown names fail safe to `restricted`."""
    return _SLOT_SENSITIVITY.get(slot_name.lower(), _DEFAULT_SENSITIVITY)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat()


def _blank(session_id: str, member_ref: Optional[str]) -> dict[str, Any]:
    now = _iso()
    return {
        "session_id": session_id,
        "member_ref": member_ref,
        "channel": "web",
        "market": "IN",
        "locale": "en-IN",
        "session_status": "open",
        "started_at": now,
        "last_activity_at": now,
        "turn_count": 0,
        "transcript": [],
        "active_flow": None,
        "queued_flows": [],
        "completed_flows": [],
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
    doc = docstore.get(session_id)
    if doc is None:
        doc = _blank(session_id, member_ref)
        docstore.put(session_id, doc)
        return doc
    if member_ref and not doc.get("member_ref"):
        doc["member_ref"] = member_ref
        docstore.put(session_id, doc)
    return doc


def get_session(session_id: str) -> Optional[dict[str, Any]]:
    return docstore.get(session_id)


def _save(doc: dict[str, Any]) -> None:
    doc["last_activity_at"] = _iso()
    doc["audit"]["state_version"] = doc["audit"].get("state_version", 0) + 1
    docstore.put(doc["session_id"], doc)


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
    """Append one turn. `turn_index` stays gap-free and per-session.

    Text is redacted before it is persisted — PII at rest, not just PII out of
    the model. The live turn is still processed against the raw message; this
    function is only ever called with what should be *kept*. Redacting here
    once, rather than at every call site, is what makes that guarantee hold.
    """
    doc = open_session(session_id, member_id)
    doc["transcript"].append({
        "turn_index": doc["turn_count"],
        "role": role,
        "content": redact(text),
        "intent": intent,
        "confidence": confidence,
        "decision_outcome": decision_outcome,
        "decision_reason_code": decision_reason_code,
        "policy_id": policy_id,
        "created_at": _iso(),
    })
    doc["turn_count"] += 1
    _save(doc)


def get_history(session_id: str) -> list[dict[str, str]]:
    """Prior turns as the {role, text} shape AgentState expects."""
    doc = docstore.get(session_id)
    if not doc:
        return []
    return [{"role": t["role"], "text": t["content"]} for t in doc["transcript"]]


def count_prior_declines(session_id: str, policy_id: str, reason_code: str) -> int:
    """How many times this session was already declined for this exact reason.

    Scoped to (policy_id, reason_code) so a decline on one policy never counts
    toward the repeat threshold of an unrelated request.
    """
    doc = docstore.get(session_id)
    if not doc:
        return 0
    return sum(
        1 for t in doc["transcript"]
        if t.get("policy_id") == policy_id
        and t.get("decision_outcome") == "DECLINE"
        and t.get("decision_reason_code") == reason_code
    )


# --------------------------------------------------------------------------- #
# Active flow and slots
# --------------------------------------------------------------------------- #
def start_flow(
    session_id: str, flow_id: str, domain: str, operation: str,
    policy_id: Optional[str] = None,
) -> dict[str, Any]:
    doc = open_session(session_id)
    doc["active_flow"] = {
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
    }
    _save(doc)
    return doc["active_flow"]


def start_flow_if_absent(session_id: str, operation: str) -> dict[str, Any]:
    """Open a slot-collecting flow for `operation`, or return the open one.

    Called the first time the agent has to ask the member for a slot. The flow
    is what makes the next turn's bare answer ("42 Brigade Road") resolvable —
    it is where the intent and the already-collected slots live between turns.
    """
    flow = get_active_flow(session_id)
    if flow and flow.get("operation") == operation:
        return flow
    doc = open_session(session_id)
    return start_flow(session_id, f"f-{doc['turn_count']}", "servicing", operation)


def get_active_flow(session_id: str) -> Optional[dict[str, Any]]:
    doc = docstore.get(session_id)
    return (doc or {}).get("active_flow")


def set_flow_status(session_id: str, status: str) -> None:
    doc = open_session(session_id)
    if doc.get("active_flow"):
        doc["active_flow"]["status"] = status
        _save(doc)


def set_slot(
    session_id: str,
    name: str,
    value: Any,
    status: str = "customer_provided",
    source: str = "member_message",
    source_ref: Optional[str] = None,
    confidence: Optional[float] = None,
) -> None:
    """Write one slot with its provenance.

    Provenance is not decoration. The escalation packet has to tell a human
    where each value came from so they do not re-ask, and "backend truth wins"
    is only enforceable if you know which values the member asserted.

    Sensitivity is deliberately NOT a parameter — it is looked up from the slot
    name, so no caller can downgrade a secret by omitting it.
    """
    doc = open_session(session_id)
    if not doc.get("active_flow"):
        start_flow(session_id, f"f-{doc['turn_count']}", "unknown", "unknown")
        doc = docstore.get(session_id)
    doc["active_flow"]["slots"][name] = {
        "value": value,
        "status": status,
        "source": source,
        "source_ref": source_ref,
        "confidence": confidence,
        "sensitivity": sensitivity_of(name),
        "collected_at": _iso(),
    }
    _save(doc)


def get_slots(session_id: str) -> dict[str, Any]:
    return (get_active_flow(session_id) or {}).get("slots", {})


def record_policy_result(session_id: str, decision: dict[str, Any]) -> None:
    doc = open_session(session_id)
    if doc.get("active_flow"):
        doc["active_flow"]["policy_results"].append(decision)
        _save(doc)


# --------------------------------------------------------------------------- #
# Loop counters — the bounds that stop a member being trapped
# --------------------------------------------------------------------------- #
def bump_clarify(session_id: str) -> int:
    doc = open_session(session_id)
    doc["dialogue"]["clarification_count"] += 1
    _save(doc)
    return doc["dialogue"]["clarification_count"]


def clarify_exhausted(session_id: str) -> bool:
    doc = docstore.get(session_id) or {}
    count = (doc.get("dialogue") or {}).get("clarification_count", 0)
    return count >= config.CLARIFY_MAX_QUESTIONS


def bump_slot_failure(session_id: str, slot: str) -> int:
    doc = open_session(session_id)
    counts = doc["dialogue"]["invalid_slot_count_by_slot"]
    counts[slot] = counts.get(slot, 0) + 1
    _save(doc)
    return counts[slot]


def slot_exhausted(session_id: str, slot: str) -> bool:
    doc = docstore.get(session_id) or {}
    counts = (doc.get("dialogue") or {}).get("invalid_slot_count_by_slot", {})
    return counts.get(slot, 0) >= config.SLOT_MAX_ATTEMPTS


def check_loop(session_id: str, signature: str) -> bool:
    """Global circuit breaker: the same state and question three times running.

    Returns True when the loop should be broken. Any change in signature resets
    the counter, so genuine progress is never penalised.
    """
    doc = open_session(session_id)
    d = doc["dialogue"]
    if d.get("loop_signature") == signature:
        d["loop_repeat_count"] = d.get("loop_repeat_count", 0) + 1
    else:
        d["loop_signature"] = signature
        d["loop_repeat_count"] = 1
    _save(doc)
    return d["loop_repeat_count"] >= 3


# --------------------------------------------------------------------------- #
# Queue and terminal
# --------------------------------------------------------------------------- #
def queue_flow(session_id: str, domain: str, operation: str) -> None:
    doc = open_session(session_id)
    doc["queued_flows"].append(
        {"domain": domain, "operation": operation, "queued_at": _iso()}
    )
    _save(doc)


def pop_queued_flow(session_id: str) -> Optional[dict[str, Any]]:
    doc = open_session(session_id)
    if not doc["queued_flows"]:
        return None
    nxt = doc["queued_flows"].pop(0)
    _save(doc)
    return nxt


def terminalize(session_id: str, outcome: str) -> None:
    """Freeze the flow, purge ephemeral sensitive slots, invalidate tokens.

    The summary is kept for context on a follow-up turn; the secrets are not.
    One rule, not two overlapping ones: anything classified `restricted` goes,
    and an unclassified slot is restricted by default.
    """
    doc = open_session(session_id)
    flow = doc.get("active_flow")
    if flow:
        kept = {
            k: v for k, v in (flow.get("slots") or {}).items()
            if (v or {}).get("sensitivity", _DEFAULT_SENSITIVITY) != "restricted"
        }
        doc["completed_flows"].append({
            "flow_id": flow.get("flow_id"),
            "domain": flow.get("domain"),
            "operation": flow.get("operation"),
            "policy_id": flow.get("policy_id"),
            "outcome": outcome,
            "slots": kept,
            "closed_at": _iso(),
        })
    doc["active_flow"] = None
    # Per-slot attempt counts belong to the flow that was asking, not to the
    # session. Leaving them set would start a second address change already at
    # its third strike.
    doc["dialogue"]["invalid_slot_count_by_slot"] = {}
    _save(doc)


def close_session(session_id: str, status: str = "closed") -> None:
    doc = open_session(session_id)
    doc["session_status"] = status
    doc["active_flow"] = None
    _save(doc)


def expire_idle() -> int:
    """Close sessions idle past the configured timeout. Returns how many.

    Expiry invalidates authentication and any pending confirmation, so a
    resumed conversation must re-authenticate.
    """
    cutoff = _now() - timedelta(seconds=config.FLOW_IDLE_TIMEOUT_S)
    n = 0
    for doc in docstore.all_sessions():
        if doc.get("session_status") != "open":
            continue
        try:
            last = datetime.fromisoformat(doc["last_activity_at"])
        except (KeyError, ValueError):
            continue
        if last < cutoff:
            doc["session_status"] = "idle"
            doc["active_flow"] = None
            docstore.put(doc["session_id"], doc)
            n += 1
    return n
