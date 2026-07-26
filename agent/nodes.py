"""agent/nodes.py — LangGraph node functions.

Each takes state, does exactly one thing, and returns updated state. No routing
logic lives here — the graph decides what runs next (see graph.py).

Imports: classifier, llm, tools, policy_engine, escalation, templates,
backend.accounts, backend.sessions, shared.security_filter, shared.observability.
"""
from __future__ import annotations

from typing import Any

from agent import escalation, templates, tools
from agent.classifier import classify, is_hardship
from agent.llm import LLMUnavailable, llm
from agent.policy_engine import evaluate
from agent.state import AgentState
from backend import accounts, sessions
from shared import security_filter as secfilter
from shared.config import config
from shared.observability import log_event
from shared.schemas import Intent, Outcome

# The slots a member must supply (directly or via auto-resolution from the read
# layer) before the request can proceed. Decision-derived slots are NOT here.
MEMBER_SLOTS: dict[str, tuple[str, ...]] = {
    "fee_waiver": ("fee_amount", "txn_ref"),
    "credit_limit_increase": (),
    "card_replacement": ("card_id",),
    "card_block": ("card_id",),
    "explain_charge": ("txn_ref",),
    "account_info": (),
    "reset_pin": (),
    "update_address": ("address",),
}

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _last_member_message(state: AgentState) -> str:
    for m in reversed(state["messages"]):
        if m.get("role") == "member":
            return m["text"]
    return ""


def _prose(system: str, user: str, fallback: str) -> str:
    """LLM prose with a deterministic fallback so offline mode still replies."""
    if llm.available():
        try:
            return llm.generate_text(system, user)
        except LLMUnavailable:
            return fallback
    return fallback


_MAX_CONTEXT_TURNS = 4


def _recent_context(state: AgentState) -> str:
    """A small, BOUNDED summary of recent turns for grounding LLM prose.

    Excludes the current (last) message — callers pass that separately. Capped
    at _MAX_CONTEXT_TURNS regardless of how long the session has run, so a
    long-running conversation never balloons the prompt: send only what's
    needed to sound continuous, not the full unbounded transcript.
    """
    prior = state["messages"][:-1][-_MAX_CONTEXT_TURNS:]
    if not prior:
        return ""
    lines = [f"{m['role']}: {m['text']}" for m in prior]
    return "Recent conversation so far:\n" + "\n".join(lines) + "\n\n"


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def security_filter(state: AgentState) -> AgentState:
    """FIRST pipeline stage — runs before classification, before any LLM call.

    Detects secrets (PIN/OTP/CVV/password/full card number) and prompt-
    injection attempts via pure pattern matching (shared/security_filter.py).
    If either fires, the turn short-circuits here: no classification, no LLM
    call, no account read. This is what keeps a real secret from ever leaving
    the process — shared/redaction.py only protects data already IN the
    pipeline (at persistence); this stops it from entering at all.
    """
    message = _last_member_message(state)

    if secfilter.contains_secret(message):
        log_event("security_event", session_id=state["session_id"],
                  member_id=state["member_id"], kind="secret_detected")
        state["reply"] = templates.render("T-SECRET-REDACTED")
        state["security_blocked"] = True
        return state

    if secfilter.contains_injection(message):
        log_event("security_event", session_id=state["session_id"],
                  member_id=state["member_id"], kind="prompt_injection")
        state["reply"] = templates.render("T-PROMPT-INJECTION")
        state["security_blocked"] = True
        return state

    state["security_blocked"] = False
    return state


def classify_intent(state: AgentState) -> AgentState:
    """Run the classifier; load account facts (incl. distress signal)."""
    message = _last_member_message(state)
    intent: Intent = classify(message)
    state["intent"] = intent
    state["confidence"] = intent.confidence

    distress = is_hardship(message, intent)
    # Facts are read once, from the read-only surface, and reused by policy +
    # escalation. distress_signals is a fact the policies can branch on.
    state["account_facts"] = accounts.get_account_facts(
        state["member_id"], distress_signals=distress
    )
    return state


def check_slots(state: AgentState) -> AgentState:
    """Auto-resolve required parameters from account data; flag what's missing.

    Most slots are resolvable from the read layer so the member only has to
    state their request. Genuinely member-provided slots (e.g. a new address)
    are marked missing so call_llm can ask for them.
    """
    intent = state["intent"].label
    slots: dict[str, Any] = dict(state.get("slots", {}))
    member_id = state["member_id"]
    member = accounts.get_member(member_id)

    if intent == "fee_waiver":
        # Find EVERY un-reversed fee — never silently pick just the newest
        # (POL-GLOBAL-005 "no silent target selection"). One match auto-
        # resolves; more than one is surfaced for the member to choose.
        fee_txns = [t for t in accounts.get_transactions(member_id, limit=20)
                    if t["kind"] == "fee" and not t["reversed"]]
        if len(fee_txns) == 1:
            slots.setdefault("fee_amount", fee_txns[0]["amount"])
            slots.setdefault("txn_ref", fee_txns[0]["txn_ref"])
        elif len(fee_txns) > 1:
            selected = state.get("selected_option")
            if selected is not None and 1 <= selected <= len(fee_txns):
                # Member answered a prior disambiguation question — resolve it.
                chosen = fee_txns[selected - 1]
                slots.setdefault("fee_amount", chosen["amount"])
                slots.setdefault("txn_ref", chosen["txn_ref"])
            else:
                # Don't pick — ask. Candidates re-derived fresh each turn (not
                # persisted structurally), so a stale choice can never apply.
                state["ambiguous_field"] = "txn_ref"
                state["ambiguous_candidates"] = [
                    {"amount": t["amount"], "txn_ref": t["txn_ref"], "date": t["date"]}
                    for t in fee_txns
                ]

    elif intent in ("card_replacement",):
        slots.setdefault("card_id", member["card_id"])

    elif intent == "card_block":
        slots.setdefault("card_id", member["card_id"])
        slots.setdefault("reason", "member reported card lost or stolen")

    elif intent == "explain_charge":
        recent = accounts.get_transactions(member_id, limit=1)
        if recent:
            slots.setdefault("txn_ref", recent[0]["txn_ref"])

    elif intent == "update_address":
        # Must come from the member — cannot be auto-resolved.
        pass

    state["slots"] = slots

    # Completeness is judged only against MEMBER-provided slots. Decision-derived
    # slots (new_limit, request_type) are filled in execute_tool once the policy
    # outcome is known, so they must not gate the conversation here. An
    # unresolved ambiguous_field also blocks completeness — same "ask, don't
    # act" routing as a missing slot.
    required = MEMBER_SLOTS.get(intent, ())
    missing = [s for s in required if slots.get(s) in (None, "")]
    state["missing_slots"] = missing
    state["slots_complete"] = len(missing) == 0 and not state.get("ambiguous_field")
    return state


def run_policy(state: AgentState) -> AgentState:
    """Evaluate the policy for this intent; append the Decision to state.

    The policy engine itself stays a pure function of account facts (no DB, no
    session awareness). Session-pattern overrides — like "this is the second
    identical decline this session" — are applied here, in the orchestration
    layer, not inside policy_engine.py.
    """
    intent = state["intent"].label
    policy_id = tools.INTENT_POLICY[intent]
    decision = evaluate(policy_id, state["account_facts"])

    if decision.outcome == Outcome.DECLINE:
        prior_declines = sessions.count_prior_declines(
            state["session_id"], policy_id, decision.reason_code
        )
        if prior_declines >= config.DECLINE_REPEAT_ESCALATE_THRESHOLD:
            decision = decision.model_copy(update={
                "outcome": Outcome.ESCALATE,
                "reason_code": "REPEATED_DECLINE_ESCALATED",
                "reason_text": (
                    "You've asked about this more than once, so rather than "
                    "decline again I'm connecting you with a specialist who can "
                    "take a closer look at your situation."
                ),
                "rule_id": "repeat_decline_escalation",
            })

    state["decision_records"] = state.get("decision_records", []) + [decision]
    return state


def call_llm(state: AgentState) -> AgentState:
    """Produce member-facing prose for clarify / ask-slots / explain-decline.

    Which prose is needed is inferred from state. Sets state['reply'] and, when
    the turn hands control back to the member, awaiting_member=True.
    """
    intent = state["intent"]
    message = _last_member_message(state)
    context = _recent_context(state)

    # 1. Below-threshold classification → ask a clarifying question.
    if intent.label == "clarify":
        state["reply"] = _prose(
            "You are a helpful, concise credit-card servicing agent.",
            f"{context}The member said: '{message}'. You are not sure what they "
            "need. Ask one short, friendly clarifying question. If the recent "
            "conversation above already answers part of it, don't ask again.",
            "I want to make sure I help with the right thing — could you tell me "
            "a bit more about what you'd like to do with your card or account?",
        )
        state["awaiting_member"] = True
        return state

    # 2. Ambiguous slot (multiple candidates) → surface options via the
    # approved template, never free LLM prose — the choices themselves are
    # facts (amounts, references) that must not be paraphrased or invented.
    if state.get("ambiguous_field"):
        candidates = state.get("ambiguous_candidates", [])
        options = "\n".join(
            f"{i + 1}. Rs {c['amount']} from {c['date'][:10]} (ref {c['txn_ref']})"
            for i, c in enumerate(candidates)
        )
        state["reply"] = templates.render("T-SELECT-ITEM", options=options)
        state["awaiting_member"] = True
        return state

    # 3. Missing slots → ask for the specific parameter.
    if not state.get("slots_complete", True):
        missing = ", ".join(state.get("missing_slots", []))  # type: ignore[arg-type]
        pretty = {"address": "your new address"}.get(missing, missing)
        state["reply"] = _prose(
            "You are a helpful, concise credit-card servicing agent.",
            f"{context}To handle the member's '{intent.label}' request you still "
            f"need: {missing}. Ask for it in one short sentence.",
            f"Sure — to continue I just need {pretty}. Could you share that?",
        )
        state["awaiting_member"] = True
        return state

    # 4. A DECLINE decision → the reply IS the policy's own reason_text,
    # verbatim. No LLM call: per "the LLM may explain a result only through an
    # approved template keyed by reason_code, it cannot invent eligibility" —
    # decision.reason_text is already the authored, versioned, policy-sourced
    # explanation (policies/*.yaml), so rewriting it through the LLM would only
    # add risk of it drifting from what the policy actually said.
    decision = state["decision_records"][-1] if state.get("decision_records") else None
    if decision and decision.outcome == Outcome.DECLINE:
        state["reply"] = decision.reason_text
        return state

    return state


def execute_tool(state: AgentState) -> AgentState:
    """Execute the tool the current decision/intent implies, via tools.execute.

    Enforces the autonomy tier inside tools.execute. If the tool is awaiting
    confirmation or step-up, records that and asks the member.
    """
    intent = state["intent"].label
    slots = dict(state.get("slots", {}))
    decision = state["decision_records"][-1] if state.get("decision_records") else None
    decision_dict = decision.model_dump() if decision else None

    tool_name = _tool_for_intent(intent, decision.outcome if decision else None)

    # Fill decision-derived slots the member never provides.
    if tool_name == "open_underwriting_case":
        slots.setdefault("request_type", intent)
    if tool_name == "adjust_credit_limit":
        facts = state["account_facts"]
        # Illustrative: approve a 20% increase over the current limit.
        member = accounts.get_member(state["member_id"])
        slots.setdefault("new_limit", round(member["credit_limit"] * 1.20, 2))

    result = tools.execute(
        tool_name,
        session_id=state["session_id"],
        member_id=state["member_id"],
        slots=slots,
        decision=decision_dict,
        confirm=state.get("confirm", False),
        reauthenticated=state.get("reauthenticated", False),
    )
    state["slots"] = slots
    state["actions_taken"] = state.get("actions_taken", []) + [result.as_record()]

    # Tier gate not satisfied → ask the member and hand control back.
    if result.status == tools.STATUS_AWAITING_CONFIRM:
        state["reply"] = (
            f"I can do that for you. To confirm: I'll {intent.replace('_', ' ')} "
            f"for card {slots.get('card_id', '')}. Shall I go ahead?"
        )
        state["awaiting_member"] = True
    elif result.status == tools.STATUS_AWAITING_STEP_UP:
        state["reply"] = (
            "For your security this action needs a quick identity re-check. "
            "Please re-authenticate and I'll complete it right away."
        )
        state["awaiting_member"] = True
    return state


def build_escalation(state: AgentState) -> AgentState:
    """Package everything into a complete-context EscalationPacket."""
    intent = state["intent"]
    decisions = state.get("decision_records", [])
    # Reason: an explicit ESCALATE decision, else hardship, else out-of-scope.
    if decisions and decisions[-1].outcome == Outcome.ESCALATE:
        reason = f"{decisions[-1].reason_code}: {decisions[-1].reason_text}"
    elif intent.label == "hardship" or state["account_facts"].get("distress_signals"):
        reason = "HARDSHIP_SIGNAL: member expressed financial distress; duty of care."
    else:
        reason = f"OUT_OF_SCOPE: '{intent.label}' is not automated; handing to a specialist."

    packet = escalation.build_escalation_packet(
        session_id=state["session_id"],
        member_id=state["member_id"],
        reason=reason,
        messages=state["messages"],
        facts_read=state["account_facts"],
        actions_attempted=state.get("actions_taken", []),
        decisions=decisions,
    )

    # If hardship, suppress collections and flag the account for sensitive handling.
    if "HARDSHIP" in reason:
        result = tools.execute(
            "flag_vulnerability",
            session_id=state["session_id"],
            member_id=state["member_id"],
            slots={"signals": ["hardship", packet.member_sentiment]},
        )
        state["actions_taken"] = state.get("actions_taken", []) + [result.as_record()]

    state["escalation_packet"] = packet
    state["escalate"] = True
    state["reply"] = (
        "I understand this is important, and I want to make sure you get the right "
        "support. I've connected you with a specialist and passed along the full "
        "context so you won't have to repeat yourself."
    )
    return state


def respond_to_member(state: AgentState) -> AgentState:
    """Format the final reply as a receipt card (on a write) or prose."""
    # An awaiting/clarify/decline turn already set a reply — keep it.
    if state.get("reply"):
        return state

    actions_taken = state.get("actions_taken", [])
    successful = [a for a in actions_taken if a.get("status") == tools.STATUS_OK]
    if successful and successful[-1].get("receipt"):
        r = successful[-1]["receipt"]
        decision = state["decision_records"][-1] if state.get("decision_records") else None
        lead = decision.reason_text if decision else ""
        state["reply"] = f"{lead}\n\n✅ {r.get('detail', '')} (ref {r.get('reference')})".strip()
    else:
        # No write happened (e.g. QUEUE with only a case opened, or read-only).
        decision = state["decision_records"][-1] if state.get("decision_records") else None
        state["reply"] = decision.reason_text if decision else "Done."
    return state


# --------------------------------------------------------------------------- #
# Intent → tool resolution (shared by check_slots and execute_tool)
# --------------------------------------------------------------------------- #
def _tool_for_intent(intent: str, outcome: Outcome | None = None) -> str:
    """Resolve the tool a given intent (and optional outcome) will invoke."""
    if intent in tools.INTENT_DIRECT_TOOL:
        return tools.INTENT_DIRECT_TOOL[intent]
    if intent in tools.INTENT_POLICY:
        if outcome == Outcome.QUEUE:
            return "open_underwriting_case"
        if outcome == Outcome.APPROVE:
            return tools.INTENT_APPROVE_TOOL[intent]
        # DECLINE/ESCALATE don't execute a write tool; return the approve tool so
        # check_slots can validate the slots it *would* need (harmless).
        return tools.INTENT_APPROVE_TOOL[intent]
    return ""  # out-of-scope intents escalate; no tool
