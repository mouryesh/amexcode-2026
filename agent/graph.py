"""agent/graph.py — assembles nodes into the runnable control flow.

The graph is the ONLY place routing logic lives. One file to read to understand
the entire control flow:

    START → classify_intent
      → [low confidence]      → call_llm (clarify)         → END (awaiting)
      → [hardship / distress] → build_escalation           → END
      → check_slots
      → [slots incomplete]    → call_llm (ask)             → END (awaiting)
      → [out-of-scope intent] → build_escalation           → END
      → [direct-tool intent]  → execute_tool → respond     → END
      → run_policy
      → [ESCALATE]            → build_escalation           → END
      → [DECLINE]             → call_llm (explain) → respond → END
      → [QUEUE]               → execute_tool → respond     → END
      → [APPROVE]             → execute_tool → respond     → END

If LangGraph is installed the real StateGraph is used; otherwise an equivalent
pure-Python runner drives the same node functions and edge predicates, so the
system runs identically in either environment.

Imports: nodes, state.
"""
from __future__ import annotations

from agent import nodes
from agent.state import AgentState
from agent.tools import INTENT_DIRECT_TOOL, INTENT_POLICY
from shared.schemas import Outcome

# --------------------------------------------------------------------------- #
# Edge predicates — shared by the LangGraph build and the fallback runner.
# --------------------------------------------------------------------------- #
def route_after_classify(state: AgentState) -> str:
    intent = state["intent"]
    if intent.label == "clarify":
        return "clarify"
    if intent.label == "hardship" or state["account_facts"].get("distress_signals"):
        return "escalate"
    return "check_slots"


def route_after_slots(state: AgentState) -> str:
    if not state.get("slots_complete", True):
        return "ask"
    intent = state["intent"].label
    if intent in INTENT_POLICY:
        return "policy"
    if intent in INTENT_DIRECT_TOOL:
        return "direct_tool"
    return "escalate"  # mapped-but-not-built categories


def route_after_policy(state: AgentState) -> str:
    outcome = state["decision_records"][-1].outcome
    return {
        Outcome.ESCALATE: "escalate",
        Outcome.DECLINE: "decline",
        Outcome.QUEUE: "queue",
        Outcome.APPROVE: "approve",
    }[outcome]


# --------------------------------------------------------------------------- #
# Fallback runner (no LangGraph dependency)
# --------------------------------------------------------------------------- #
def _run_fallback(state: AgentState) -> AgentState:
    state = nodes.classify_intent(state)
    branch = route_after_classify(state)

    if branch == "clarify":
        return nodes.call_llm(state)          # awaiting member
    if branch == "escalate":
        return nodes.build_escalation(state)

    state = nodes.check_slots(state)
    branch = route_after_slots(state)
    if branch == "ask":
        return nodes.call_llm(state)          # awaiting member
    if branch == "escalate":
        return nodes.build_escalation(state)
    if branch == "direct_tool":
        state = nodes.execute_tool(state)
        return nodes.respond_to_member(state)

    # policy path
    state = nodes.run_policy(state)
    branch = route_after_policy(state)
    if branch == "escalate":
        return nodes.build_escalation(state)
    if branch == "decline":
        state = nodes.call_llm(state)         # explain decline
        return nodes.respond_to_member(state)
    # queue or approve both execute a tool then respond
    state = nodes.execute_tool(state)
    return nodes.respond_to_member(state)


# --------------------------------------------------------------------------- #
# LangGraph build (used when the library is available)
# --------------------------------------------------------------------------- #
def _build_langgraph():
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(AgentState)
    g.add_node("classify_intent", nodes.classify_intent)
    g.add_node("check_slots", nodes.check_slots)
    g.add_node("run_policy", nodes.run_policy)
    g.add_node("call_llm", nodes.call_llm)
    g.add_node("execute_tool", nodes.execute_tool)
    g.add_node("build_escalation", nodes.build_escalation)
    g.add_node("respond_to_member", nodes.respond_to_member)

    g.add_edge(START, "classify_intent")
    g.add_conditional_edges(
        "classify_intent",
        route_after_classify,
        {"clarify": "call_llm", "escalate": "build_escalation", "check_slots": "check_slots"},
    )
    g.add_conditional_edges(
        "check_slots",
        route_after_slots,
        {"ask": "call_llm", "policy": "run_policy",
         "direct_tool": "execute_tool", "escalate": "build_escalation"},
    )
    g.add_conditional_edges(
        "run_policy",
        route_after_policy,
        {"escalate": "build_escalation", "decline": "call_llm",
         "queue": "execute_tool", "approve": "execute_tool"},
    )

    # call_llm either awaits the member (clarify/ask) or explains a decline.
    def after_call_llm(state: AgentState) -> str:
        return END if state.get("awaiting_member") else "respond_to_member"

    g.add_conditional_edges("call_llm", after_call_llm,
                            {END: END, "respond_to_member": "respond_to_member"})
    g.add_edge("execute_tool", "respond_to_member")
    g.add_edge("respond_to_member", END)
    g.add_edge("build_escalation", END)
    return g.compile()


class _Runnable:
    """Uniform .invoke(state) wrapper over whichever backend is available."""

    def __init__(self) -> None:
        try:
            self._graph = _build_langgraph()
            self.backend = "langgraph"
        except Exception:
            self._graph = None
            self.backend = "fallback"

    def invoke(self, state: AgentState) -> AgentState:
        if self._graph is not None:
            return self._graph.invoke(state)
        return _run_fallback(state)


# Compiled once, reused per request.
GRAPH = _Runnable()


def run(state: AgentState) -> AgentState:
    """Entry point used by api/routes.py."""
    return GRAPH.invoke(state)
