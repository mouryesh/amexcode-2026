"""api/routes.py — three endpoints. No business logic; pure HTTP<->schema translation.

Imports: agent.graph, backend.ledger, backend.sessions, shared.schemas,
shared.exceptions, shared.observability.
"""
from __future__ import annotations

import hashlib
import time
import uuid

from fastapi import APIRouter, HTTPException

from agent.graph import run
from agent.state import new_state
from backend import ledger, sessions
from shared.exceptions import ServicingError
from shared.observability import log_event
from shared.redaction import redact
from shared.schemas import AgentRequest, AgentResponse

router = APIRouter()


def _audit_turn(req: AgentRequest, result: dict, trace_id: str) -> None:
    """Write the per-turn audit rows for one message.

    This is what makes a DECLINE auditable. Previously only backend/actions.py
    appended, so an approved waiver left a trail and a refused one left nothing
    — and a refusal is exactly what a member disputes later. Every turn now
    writes, whether or not anything was mutated.

    The message is stored redacted, with a hash of the redacted form so two
    identical asks are comparable without keeping the raw text around.
    """
    intent = result.get("intent")
    safe = redact(req.message)
    ledger.emit_turn(
        session_id=req.session_id,
        member_ref=req.member_id,
        role="member",
        action="message_received",
        payload={
            "message_redacted": safe,
            "message_sha256": hashlib.sha256(safe.encode("utf-8")).hexdigest(),
            "intent": intent.label if intent else None,
            "confidence": round(intent.confidence, 4) if intent else None,
            "confirm": req.confirm,
            "reauthenticated": req.reauthenticated,
            "selected_option": req.selected_option,
        },
        trace_id=trace_id,
    )

    # One row per policy evaluation, carrying the exact facts it read so an
    # auditor can re-run evaluate() and reproduce the outcome.
    for decision in result.get("decision_records") or []:
        d = decision.model_dump(mode="json")
        ledger.emit_policy(
            session_id=req.session_id,
            member_ref=req.member_id,
            decision=d,
            inputs_used=d.get("inputs_used") or {},
            trace_id=trace_id,
        )

    if result.get("security_event"):
        ledger.emit_security(
            session_id=req.session_id,
            member_ref=req.member_id,
            action=str(result["security_event"]),
            detail={"redaction_applied": True},
            trace_id=trace_id,
        )

    ledger.emit_turn(
        session_id=req.session_id,
        member_ref=req.member_id,
        role="agent",
        action="reply_rendered",
        payload={
            "reply": result.get("reply", ""),
            "escalate": result.get("escalate", False),
            "awaiting_member": result.get("awaiting_member", False),
            "actions_taken": [a.get("tool") for a in result.get("actions_taken") or []],
        },
        trace_id=trace_id,
    )

    # Terminal only when the agent is not waiting on the member — an awaiting
    # turn is mid-flow, not an outcome.
    if not result.get("awaiting_member"):
        records = result.get("decision_records") or []
        outcome = records[-1].outcome.value if records else (
            "ESCALATED" if result.get("escalate") else "CLOSED"
        )
        ledger.emit_terminal(
            session_id=req.session_id,
            member_ref=req.member_id,
            outcome=outcome,
            detail={
                "receipts": [a.get("reference") for a in result.get("actions_taken") or []],
                "escalated": result.get("escalate", False),
            },
            trace_id=trace_id,
        )


@router.post("/agent/message", response_model=AgentResponse)
def agent_message(req: AgentRequest) -> AgentResponse:
    """Run the full pipeline for one member message."""
    start = time.perf_counter()
    trace_id = uuid.uuid4().hex
    history = sessions.get_history(req.session_id)
    state = new_state(
        session_id=req.session_id,
        member_id=req.member_id,
        message=req.message,
        confirm=req.confirm,
        reauthenticated=req.reauthenticated,
        history=history,
        selected_option=req.selected_option,
    )
    try:
        result = run(state)
    except ServicingError as exc:
        log_event(
            "agent_message",
            session_id=req.session_id,
            member_id=req.member_id,
            success=False,
            error_type=type(exc).__name__,
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        raise HTTPException(status_code=exc.http_status, detail=str(exc))

    decision = result.get("decision_records") or []
    last_decision = decision[-1] if decision else None
    intent = result.get("intent")

    log_event(
        "agent_message",
        session_id=req.session_id,
        member_id=req.member_id,
        intent=intent.label if intent else None,
        confidence=round(intent.confidence, 3) if intent else None,
        outcome=last_decision.outcome.value if last_decision else None,
        escalate=result.get("escalate", False),
        awaiting_member=result.get("awaiting_member", False),
        success=True,
        latency_ms=round((time.perf_counter() - start) * 1000, 1),
    )

    sessions.record_turn(
        req.session_id, req.member_id, "member", req.message,
        intent=intent.label if intent else None,
        confidence=intent.confidence if intent else None,
    )
    sessions.record_turn(
        req.session_id, req.member_id, "agent", result.get("reply", ""),
        decision_outcome=last_decision.outcome.value if last_decision else None,
        decision_reason_code=last_decision.reason_code if last_decision else None,
        policy_id=last_decision.policy_id if last_decision else None,
    )

    _audit_turn(req, result, trace_id)

    return AgentResponse(
        reply=result.get("reply", ""),
        actions_taken=result.get("actions_taken", []),
        decision_records=result.get("decision_records", []),
        escalate=result.get("escalate", False),
        escalation_packet=result.get("escalation_packet"),
        intent=result.get("intent"),
        awaiting_member=result.get("awaiting_member", False),
    )


# NOTE: /audit/verify is declared BEFORE /audit/{session_id} so FastAPI does not
# match the literal "verify" as a session id.
@router.get("/audit/verify")
def audit_verify() -> dict:
    """Walk the chain and return OK or TAMPERED with the broken record id."""
    return ledger.verify()


@router.get("/audit/{session_id}")
def audit_session(session_id: str) -> dict:
    """Return every ledger record for this session, in chain order."""
    return {"session_id": session_id, "records": ledger.records_for_session(session_id)}
