"""shared/schemas.py — every Pydantic model in the system.

This is the executable version of the contracts. If a data shape is not
defined here, it is not a contract and must not cross a layer boundary.

Imported by: everything.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def _now_iso() -> str:
    """Single source of truth for timestamps (UTC, ISO-8601)."""
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Outcome(str, Enum):
    """The only four things a policy can decide."""

    APPROVE = "APPROVE"
    DECLINE = "DECLINE"
    QUEUE = "QUEUE"
    ESCALATE = "ESCALATE"


class AutonomyTier(str, Enum):
    """Declared per action, enforced in code (tools.py), never a model judgment."""

    AUTO = "auto"
    AUTO_STEP_UP = "auto_step_up"
    PROPOSE_CONFIRM = "propose_confirm"
    ESCALATE = "escalate"


# --------------------------------------------------------------------------- #
# Classifier output
# --------------------------------------------------------------------------- #
class Intent(BaseModel):
    """Classified intent label + confidence score."""

    label: str = Field(..., description="One of the 12 taxonomy categories, or 'clarify'.")
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: Optional[str] = Field(None, description="Short model explanation (debug only).")


# --------------------------------------------------------------------------- #
# Policy engine output
# --------------------------------------------------------------------------- #
class Decision(BaseModel):
    """Self-explaining policy engine output.

    Carries everything needed to reconstruct *why* an outcome was reached:
    the outcome, a machine reason code, human reason text, the policy that
    produced it, its version, and every account fact the rule actually read.
    """

    outcome: Outcome
    reason_code: str
    reason_text: str
    policy_id: str
    policy_version: str
    inputs_used: dict[str, Any] = Field(default_factory=dict)
    rule_id: Optional[str] = Field(None, description="Which rule branch fired.")
    # Provenance of the rule that fired: is this a real, sourced Amex India
    # public rule, or an illustrative control invented for this prototype?
    # See policies/*.yaml — every rule declares its own source_tag/source_ref.
    source_tag: Optional[str] = Field(
        None, description="amex_public | regulatory_public | project_control | ..."
    )
    source_ref: Optional[str] = Field(
        None, description="URL or internal reference backing this rule."
    )


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
class AuditRecord(BaseModel):
    """The exact shape of one append-only, hash-chained ledger row."""

    record_id: Optional[int] = None
    session_id: Optional[str] = None
    timestamp: str = Field(default_factory=_now_iso)
    actor: str
    action: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    decision: Optional[dict[str, Any]] = None
    prev_hash: str
    record_hash: str


# --------------------------------------------------------------------------- #
# Write-action receipt
# --------------------------------------------------------------------------- #
class Receipt(BaseModel):
    """Returned to the member after a successful write action."""

    reference: str
    action: str
    amount: Optional[float] = None
    currency: str = "INR"
    timestamp: str = Field(default_factory=_now_iso)
    new_balance: Optional[float] = None
    detail: Optional[str] = None


# --------------------------------------------------------------------------- #
# Escalation
# --------------------------------------------------------------------------- #
class EscalationPacket(BaseModel):
    """The structured handoff a human agent receives.

    A stranger should be able to act on this in ten seconds.
    """

    session_id: str
    member_id: str
    reason: str = Field(..., description="Why the agent stopped and escalated.")
    transcript: list[dict[str, str]] = Field(default_factory=list)
    facts_read: dict[str, Any] = Field(default_factory=dict)
    actions_attempted: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    member_sentiment: str = "neutral"
    suggested_next_action: str = ""
    created_at: str = Field(default_factory=_now_iso)


# --------------------------------------------------------------------------- #
# API boundary
# --------------------------------------------------------------------------- #
class AgentRequest(BaseModel):
    """POST /agent/message input.

    Confirmation contract (the graph is stateless per turn): when a previous
    response has `awaiting_member=True` for a propose_confirm or auto_step_up
    action, the client resubmits the SAME `message` with `confirm=true`
    (or `reauthenticated=true`) to complete it. This keeps session state out of
    the agent and in the caller, and makes every turn independently replayable.

    The same pattern covers disambiguation: when a response asks the member to
    choose among numbered options (T-SELECT-ITEM), the client resubmits the
    SAME `message` with `selected_option` set to the chosen number.
    """

    session_id: str
    member_id: str
    message: str
    # Set by the member UI when confirming a propose_confirm action, or when
    # completing a step-up re-authentication challenge.
    confirm: bool = False
    reauthenticated: bool = False
    selected_option: Optional[int] = None


class AgentResponse(BaseModel):
    """POST /agent/message output."""

    reply: str
    actions_taken: list[dict[str, Any]] = Field(default_factory=list)
    decision_records: list[Decision] = Field(default_factory=list)
    escalate: bool = False
    escalation_packet: Optional[EscalationPacket] = None
    intent: Optional[Intent] = None
    # True when the agent is waiting on the member (clarify / confirm / step-up).
    awaiting_member: bool = False
