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
    #
    # slots_complete / missing_slots MUST be declared here, not set ad-hoc:
    # LangGraph builds its state schema from AgentState's declared fields —
    # an undeclared dict key gets silently dropped between node calls (it
    # survives within one node's own execution, so this only shows up once a
    # downstream node reads it after a graph hop).
    slots: dict[str, Any]
    slots_complete: bool
    missing_slots: list[str]
    # True when a slot has been asked for SLOT_MAX_ATTEMPTS times without a
    # usable answer. The graph routes this to escalation rather than asking a
    # fourth time — an unbounded ask loop is a member trapped with no human.
    slots_exhausted: bool
    # Slot name -> value the extractor filled this turn, kept for the audit row
    # so a reviewer can see what the model read out of free text.
    slots_extracted: dict[str, Any]

    # Conversation -----------------------------------------------------------
    # Each message: {"role": "member"|"agent", "text": "..."}
    messages: list[dict[str, str]]

    # Signals the member UI sets on the request.
    confirm: bool          # member confirmed a propose_confirm action
    reauthenticated: bool  # member cleared an auto_step_up challenge
    selected_option: Optional[int]  # member's numbered answer to a disambiguation question

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

    # Security -----------------------------------------------------------
    # True when security_filter (the first pipeline stage) detected a secret
    # or an injection attempt and short-circuited the turn — nothing past that
    # node runs: no classification, no LLM call, no account read.
    security_blocked: bool

    # Fail-closed ------------------------------------------------------------
    # Set by nodes.fail_closed to the catalogue operation that was correctly
    # recognised but has no approved policy. Carried into the escalation packet
    # so the handoff names the exact operation and its owning flow.
    unmapped_operation: Optional[str]
    # Set when the loop detector breaks a repeating exchange.
    loop_broken: bool

    # Disambiguation -----------------------------------------------------
    # Set by check_slots when a slot has MORE THAN ONE plausible candidate
    # (e.g. two unreversed fees). Never silently picked — the member is asked
    # which one via a numbered T-SELECT-ITEM template, and answers by
    # resubmitting the same message with `selected_option` set.
    ambiguous_field: Optional[str]
    ambiguous_candidates: list[dict[str, Any]]


def new_state(session_id: str, member_id: str, message: str,
              confirm: bool = False, reauthenticated: bool = False,
              history: list[dict[str, str]] | None = None,
              selected_option: int | None = None) -> AgentState:
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
        slots_complete=True,
        missing_slots=[],
        slots_exhausted=False,
        slots_extracted={},
        messages=messages,
        confirm=confirm,
        reauthenticated=reauthenticated,
        selected_option=selected_option,
        account_facts={},
        decision_records=[],
        actions_taken=[],
        escalate=False,
        escalation_packet=None,
        reply="",
        awaiting_member=False,
        security_blocked=False,
        unmapped_operation=None,
        loop_broken=False,
        ambiguous_field=None,
        ambiguous_candidates=[],
    )
