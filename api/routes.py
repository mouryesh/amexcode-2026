"""api/routes.py — three endpoints. No business logic; pure HTTP<->schema translation.

Imports: agent.graph, backend.ledger, backend.sessions, shared.schemas, shared.exceptions.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from agent.graph import run
from agent.state import new_state
from backend import ledger, sessions
from shared.exceptions import ServicingError
from shared.schemas import AgentRequest, AgentResponse

router = APIRouter()


@router.post("/agent/message", response_model=AgentResponse)
def agent_message(req: AgentRequest) -> AgentResponse:
    """Run the full LangGraph pipeline for one member message."""
    history = sessions.get_history(req.session_id)
    state = new_state(
        session_id=req.session_id,
        member_id=req.member_id,
        message=req.message,
        confirm=req.confirm,
        reauthenticated=req.reauthenticated,
        history=history,
    )
    try:
        result = run(state)
    except ServicingError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc))

    decision = result.get("decision_records") or []
    last_decision = decision[-1] if decision else None
    intent = result.get("intent")

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
