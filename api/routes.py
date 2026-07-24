"""api/routes.py — three endpoints. No business logic; pure HTTP<->schema translation.

Imports: agent.graph, backend.ledger, shared.schemas, shared.exceptions.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from agent.graph import run
from agent.state import new_state
from backend import ledger
from shared.exceptions import ServicingError
from shared.schemas import AgentRequest, AgentResponse

router = APIRouter()


@router.post("/agent/message", response_model=AgentResponse)
def agent_message(req: AgentRequest) -> AgentResponse:
    """Run the full LangGraph pipeline for one member message."""
    state = new_state(
        session_id=req.session_id,
        member_id=req.member_id,
        message=req.message,
        confirm=req.confirm,
        reauthenticated=req.reauthenticated,
    )
    try:
        result = run(state)
    except ServicingError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc))

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
