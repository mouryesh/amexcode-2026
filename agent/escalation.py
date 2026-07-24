"""agent/escalation.py — builds the EscalationPacket.

The structured handoff a human agent receives. This is a scored deliverable and
a first-class artifact: a stranger should be able to act on it in ten seconds.
It is not a side effect of ending a conversation.

Imports: shared.schemas only.
"""
from __future__ import annotations

from typing import Any

from shared.schemas import Decision, EscalationPacket

# Very small lexicon for sentiment — enough to surface tone to the human agent
# without pulling in an ML dependency.
_NEGATIVE = ["can't", "cannot", "lost my job", "struggling", "worried", "stressed",
             "angry", "frustrated", "unfair", "desperate", "scared", "afraid",
             "help", "please", "behind"]
_POSITIVE = ["thanks", "thank you", "great", "appreciate", "perfect", "awesome"]


def detect_sentiment(messages: list[dict[str, str]]) -> str:
    """Rough member sentiment from their own turns."""
    text = " ".join(m["text"].lower() for m in messages if m.get("role") == "member")
    neg = sum(1 for w in _NEGATIVE if w in text)
    pos = sum(1 for w in _POSITIVE if w in text)
    if neg > pos and neg > 0:
        return "distressed" if any(w in text for w in
                                   ["lost my job", "can't", "cannot", "desperate"]) else "negative"
    if pos > neg and pos > 0:
        return "positive"
    return "neutral"


def _suggest_next_action(reason: str, sentiment: str) -> str:
    if sentiment in ("distressed", "negative") or "HARDSHIP" in reason.upper():
        return (
            "Review for financial-relief programme eligibility. Do NOT pursue "
            "collections. Confirm vulnerability flag is set and offer a hardship "
            "specialist call-back."
        )
    if "FRAUD" in reason.upper():
        return "Route to the fraud team; verify identity before any card action."
    return "Review the request and the cited policy decision, then action or decline with reason."


def build_escalation_packet(
    session_id: str,
    member_id: str,
    reason: str,
    messages: list[dict[str, str]],
    facts_read: dict[str, Any],
    actions_attempted: list[dict[str, Any]],
    decisions: list[Decision],
) -> EscalationPacket:
    """Assemble the complete-context handoff document."""
    sentiment = detect_sentiment(messages)
    return EscalationPacket(
        session_id=session_id,
        member_id=member_id,
        reason=reason,
        transcript=list(messages),
        facts_read=dict(facts_read),
        actions_attempted=list(actions_attempted),
        decisions=list(decisions),
        member_sentiment=sentiment,
        suggested_next_action=_suggest_next_action(reason, sentiment),
    )
