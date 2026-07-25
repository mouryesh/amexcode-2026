"""agent/state.py — the single mutable object threaded through the graph.

Defines the AgentState TypedDict LangGraph passes between nodes. Everything
reads from and writes to this object. No globals, no side channels.

Imports: shared.schemas.
"""
from __future__ import annotations

from typing import Any, Optional, TypedDict

from shared.schemas import Decision, EscalationPacket, Intent


class AgentState(TypedDict, total=False):
    # Identity ---------------------------------------------------------------
    session_id: str
    member_id: str

    # Classification ---------------------------------------------------------
    intent: Optional[Intent]
    confidence: float

    # Slot filling -----------------------------------------------------------
    # Collected parameters for the classified intent (fee_amount, txn_ref,
    # card_id, new_limit, ...). check_slots decides when this is complete.
    slots: dict[str, Any]

    # Conversation -----------------------------------------------------------
    # Each message: {"role": "member"|"agent", "text": "..."}
    messages: list[dict[str, str]]

    # Signals the member UI sets on the request.
    confirm: bool          # member confirmed a propose_confirm action
    reauthenticated: bool  # member cleared an auto_step_up challenge

    # Working records --------------------------------------------------------
    account_facts: dict[str, Any]
    decision_records: list[Decision]
    actions_taken: list[dict[str, Any]]

    # Outcome ----------------------------------------------------------------
    escalate: bool
    escalation_packet: Optional[EscalationPacket]
    reply: str
    # Set when the agent has produced a turn and is waiting on the member
    # (clarify / confirm / step-up) rather than completing the request.
    awaiting_member: bool


def new_state(session_id: str, member_id: str, message: str,
              confirm: bool = False, reauthenticated: bool = False,
              history: list[dict[str, str]] | None = None) -> AgentState:
    """Build a fresh state for one member message.

    `history` is the session's prior turns (from backend.sessions.get_history),
    threaded in ahead of the current message so nodes that read state["messages"]
    — sentiment detection, the escalation transcript — see the whole
    conversation, not just this turn.
    """
    messages = list(history) if history else []
    messages.append({"role": "member", "text": message})
    return AgentState(
        session_id=session_id,
        member_id=member_id,
        intent=None,
        confidence=0.0,
        slots={},
        messages=messages,
        confirm=confirm,
        reauthenticated=reauthenticated,
        account_facts={},
        decision_records=[],
        actions_taken=[],
        escalate=False,
        escalation_packet=None,
        reply="",
        awaiting_member=False,
    )
