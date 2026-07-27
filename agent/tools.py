"""agent/tools.py — the tool definitions the LLM is allowed to call.

Each tool is a thin wrapper that does three things:
  1. Validates input against the expected schema.
  2. Enforces the autonomy tier (auto / auto_step_up / propose_confirm /
     escalate) — checked here in code, NEVER trusted to the model.
  3. Delegates to the appropriate function in backend/actions.py.

Imports: backend.actions, agent.policy_engine, shared.schemas, shared.config.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from backend import actions
from shared.schemas import AutonomyTier, Receipt

# --------------------------------------------------------------------------- #
# Tool result — the shape every tool returns to nodes.execute_tool.
# --------------------------------------------------------------------------- #
STATUS_OK = "ok"
STATUS_AWAITING_CONFIRM = "awaiting_confirm"   # propose_confirm, member hasn't confirmed
STATUS_AWAITING_STEP_UP = "awaiting_step_up"   # auto_step_up, not re-authenticated
STATUS_ERROR = "error"


@dataclass
class ToolResult:
    status: str
    tool: str
    tier: AutonomyTier
    receipt: Optional[Receipt] = None
    message: str = ""

    def as_record(self) -> dict[str, Any]:
        """Flatten to the dict appended to state.actions_taken / ledger view."""
        rec: dict[str, Any] = {
            "tool": self.tool,
            "tier": self.tier.value,
            "status": self.status,
        }
        if self.receipt is not None:
            rec["receipt"] = self.receipt.model_dump()
        if self.message:
            rec["message"] = self.message
        return rec


# --------------------------------------------------------------------------- #
# Tool registry
# --------------------------------------------------------------------------- #
@dataclass
class Tool:
    name: str
    tier: AutonomyTier
    required_slots: tuple[str, ...]
    run: Callable[..., Receipt]
    read_only: bool = False


def _idem_key(session_id: str, tool: str) -> str:
    """One write per (session, tool) — replaying a turn never double-executes."""
    return f"{session_id}:{tool}"


# The ~15 tools. Return-shape and tier are declared here so the graph can reason
# about autonomy without inspecting the implementation.
TOOLS: dict[str, Tool] = {
    "reverse_fee": Tool(
        "reverse_fee", AutonomyTier.AUTO, ("fee_amount", "txn_ref"),
        run=actions.reverse_fee,
    ),
    "block_card": Tool(
        # `reason` must be declared, not just present in slots: execute() only
        # forwards the kwargs a tool declares, so omitting it here dropped the
        # argument and blew up inside actions.block_card.
        "block_card", AutonomyTier.AUTO, ("card_id", "reason"),
        run=actions.block_card,
    ),
    "issue_replacement": Tool(
        "issue_replacement", AutonomyTier.PROPOSE_CONFIRM, ("card_id",),
        run=actions.issue_replacement,
    ),
    "adjust_credit_limit": Tool(
        "adjust_credit_limit", AutonomyTier.AUTO, ("new_limit",),
        run=actions.adjust_credit_limit,
    ),
    "open_underwriting_case": Tool(
        "open_underwriting_case", AutonomyTier.AUTO, ("request_type",),
        run=actions.open_underwriting_case,
    ),
    "flag_vulnerability": Tool(
        "flag_vulnerability", AutonomyTier.AUTO, ("signals",),
        run=actions.flag_vulnerability,
    ),
    "reset_pin": Tool(
        "reset_pin", AutonomyTier.AUTO_STEP_UP, (),
        run=lambda **kw: Receipt(reference="PIN-RESET", action="reset_pin",
                                 detail="PIN reset link sent after re-authentication."),
    ),
    "update_address": Tool(
        "update_address", AutonomyTier.AUTO_STEP_UP, ("address",),
        run=lambda **kw: Receipt(reference="ADDR-UPD", action="update_address",
                                 detail="Address updated after re-authentication."),
    ),
    "explain_charge": Tool(
        "explain_charge", AutonomyTier.AUTO, ("txn_ref",),
        run=lambda **kw: Receipt(reference="EXPLAIN", action="explain_charge",
                                 detail="Charge detail returned."),
        read_only=True,
    ),
    "get_account_info": Tool(
        "get_account_info", AutonomyTier.AUTO, (),
        run=lambda **kw: Receipt(reference="INFO", action="get_account_info",
                                 detail="Account info returned."),
        read_only=True,
    ),
}


# --------------------------------------------------------------------------- #
# Tier enforcement
# --------------------------------------------------------------------------- #
def _check_tier(tool: Tool, confirm: bool, reauthenticated: bool) -> Optional[str]:
    """Return an 'awaiting' status if the tier's precondition isn't met, else None.

    This is the load-bearing safety check: whether an action needs confirmation
    or re-authentication is decided by the declared tier, not by the model.
    """
    if tool.tier == AutonomyTier.PROPOSE_CONFIRM and not confirm:
        return STATUS_AWAITING_CONFIRM
    if tool.tier == AutonomyTier.AUTO_STEP_UP and not reauthenticated:
        return STATUS_AWAITING_STEP_UP
    return None


def _validate_slots(tool: Tool, slots: dict[str, Any]) -> None:
    missing = [s for s in tool.required_slots if slots.get(s) in (None, "")]
    if missing:
        raise ValueError(f"Tool '{tool.name}' missing required slots: {missing}")


def execute(
    tool_name: str,
    *,
    session_id: str,
    member_id: str,
    slots: dict[str, Any],
    decision: Optional[dict[str, Any]] = None,
    confirm: bool = False,
    reauthenticated: bool = False,
) -> ToolResult:
    """Validate, enforce tier, then delegate to backend/actions.

    Never bypasses the tier check. A propose_confirm/auto_step_up tool that
    hasn't met its precondition returns an 'awaiting' result and performs NO
    write.
    """
    if tool_name not in TOOLS:
        return ToolResult(STATUS_ERROR, tool_name, AutonomyTier.AUTO,
                          message=f"Unknown tool '{tool_name}'.")
    tool = TOOLS[tool_name]

    gate = _check_tier(tool, confirm, reauthenticated)
    if gate is not None:
        return ToolResult(gate, tool.name, tool.tier)

    _validate_slots(tool, slots)

    # Read-only tools don't write or touch idempotency.
    if tool.read_only:
        receipt = tool.run(member_id=member_id, **slots)
        return ToolResult(STATUS_OK, tool.name, tool.tier, receipt=receipt)

    # Build the delegated call. Only pass kwargs the backend function accepts;
    # the write functions all take member_id + idempotency_key + session_id.
    kwargs: dict[str, Any] = {
        "member_id": member_id,
        "idempotency_key": _idem_key(session_id, tool.name),
        "session_id": session_id,
    }
    kwargs.update({s: slots[s] for s in tool.required_slots})
    if decision is not None and tool.name in (
        "reverse_fee", "adjust_credit_limit", "open_underwriting_case"
    ):
        kwargs["decision"] = decision

    receipt = tool.run(**kwargs)
    return ToolResult(STATUS_OK, tool.name, tool.tier, receipt=receipt)


# --------------------------------------------------------------------------- #
# Intent → policy / tool routing (data, so the graph stays declarative)
# --------------------------------------------------------------------------- #
INTENT_POLICY: dict[str, str] = {
    "fee_waiver": "fee.late.courtesy_waiver",
    "credit_limit_increase": "credit.limit_increase",
    "card_replacement": "card.replacement",
}

# The tool that executes an APPROVE for each policy-backed intent.
INTENT_APPROVE_TOOL: dict[str, str] = {
    "fee_waiver": "reverse_fee",
    "credit_limit_increase": "adjust_credit_limit",
    "card_replacement": "issue_replacement",
}

# Intents that resolve directly with a tool, no policy evaluation needed.
INTENT_DIRECT_TOOL: dict[str, str] = {
    "card_block": "block_card",
    "explain_charge": "explain_charge",
    "account_info": "get_account_info",
    "reset_pin": "reset_pin",
    "update_address": "update_address",
}
