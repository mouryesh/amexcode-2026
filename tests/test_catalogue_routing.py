"""tests/test_catalogue_routing.py — all 380 operations route deterministically.

The claim under test is not "the agent can do 380 things" — six are automated.
It is POL-GLOBAL-011: every catalogue operation reaches a defined, safe branch,
and no operation without an approved policy can reach a write.

The exhaustive test is the one that matters: it walks the entire catalogue and
asserts each operation lands in exactly one of four branches, so adding an
operation to the YAML can never silently create an unhandled path.
"""
import pytest

from agent import catalogue, nodes
from agent.classifier import LABEL_TO_OPERATION, OPERATION_TO_LABEL, label_for
from agent.graph import route_after_slots
from agent.tools import INTENT_DIRECT_TOOL
from backend import policy_registry
from backend.seed import seed
from shared.schemas import Intent


@pytest.fixture(autouse=True)
def _seeded():
    seed()


def _state(operation, facts=None):
    """Minimal post-slot state for a routing decision."""
    return {
        "session_id": "route", "member_id": "MEM-PRIYA",
        "intent": Intent(label=label_for(operation), confidence=0.95, operation=operation),
        "account_facts": facts if facts is not None
        else {"market": "IN", "product_family": "personal_credit"},
        "slots_complete": True, "missing_slots": [], "slots_exhausted": False,
    }


# --------------------------------------------------------------------------- #
# The catalogue itself
# --------------------------------------------------------------------------- #
def test_catalogue_is_complete_and_consistent():
    ops = catalogue.operations()
    assert len(ops) == 380, len(ops)
    assert len(catalogue.domains()) == 27

    for op_id, op in ops.items():
        assert op_id == f"{op.domain}.{op.operation}"
        assert op.state_profile.startswith("ST-")
        assert op.flow.startswith("FL-")
        # One domain, one profile, one flow — the spine the three specs share.
        assert op.state_profile[3:] == op.flow[3:], op_id


def test_unknown_operation_is_treated_as_consequential():
    """Fail safe: an id not in the catalogue must never be assumed read-only."""
    assert catalogue.is_consequential("not_a_domain.not_an_operation")
    assert catalogue.resolve("not_a_domain.not_an_operation") is None


# --------------------------------------------------------------------------- #
# The exhaustive guarantee
# --------------------------------------------------------------------------- #
def test_every_operation_routes_to_exactly_one_defined_branch():
    branches = {"policy", "direct_tool", "inform", "fail_closed"}
    seen = {}
    for op_id in catalogue.operations():
        branch = route_after_slots(_state(op_id))
        assert branch in branches, f"{op_id} routed to unknown branch {branch!r}"
        seen.setdefault(branch, []).append(op_id)

    assert sum(len(v) for v in seen.values()) == 380
    # Every branch is exercised by the real catalogue, not just reachable in theory.
    assert set(seen) == branches, f"unused branch(es): {branches - set(seen)}"


def test_no_consequential_operation_without_a_policy_can_reach_a_tool():
    """The single most important property in the graph."""
    for op_id, op in catalogue.operations().items():
        if not op.consequential:
            continue
        if policy_registry.try_resolve(op_id) is not None:
            continue
        if label_for(op_id) in INTENT_DIRECT_TOOL:
            continue  # a built tool, deliberately automated
        assert route_after_slots(_state(op_id)) == "fail_closed", op_id


def test_read_only_without_policy_informs_rather_than_escalating():
    op_id = "membership_rewards.points_expiry"
    assert not catalogue.is_consequential(op_id)
    assert route_after_slots(_state(op_id)) == "inform"

    state = _state(op_id)
    nodes.inform_from_source(state)
    assert "points expiry" in state["reply"]
    assert not state.get("escalate")
    assert not state.get("actions_taken")


def test_fail_closed_escalates_and_names_the_operation():
    op_id = "payflex_emi.foreclose_emi"
    state = _state(op_id)
    state.update({"messages": [{"role": "member", "text": "foreclose my emi"}],
                  "decision_records": [], "actions_taken": []})
    nodes.fail_closed(state)

    assert state["escalate"]
    assert state["unmapped_operation"] == op_id
    assert not [a for a in state["actions_taken"] if a.get("status") == "ok"]
    reason = state["escalation_packet"].reason
    assert "POLICY_NOT_BOUND" in reason and op_id in reason
    assert "FL-EMI" in reason, "the handoff should name the owning flow"


# --------------------------------------------------------------------------- #
# The registry and the catalogue must agree
# --------------------------------------------------------------------------- #
def test_every_bound_policy_names_a_real_catalogue_operation():
    for op_id in policy_registry.bound_intents():
        assert catalogue.exists(op_id), f"{op_id} is bound but not in the catalogue"


def test_every_built_label_maps_to_a_real_operation():
    for label, op_id in LABEL_TO_OPERATION.items():
        assert catalogue.exists(op_id), f"{label} -> {op_id} is not in the catalogue"
    for op_id in OPERATION_TO_LABEL:
        assert catalogue.exists(op_id), op_id


def test_policy_dispatch_is_scoped_by_product_family():
    """A credit-limit increase is bound for personal_credit only."""
    cli = "credit_spending_power.permanent_credit_limit_increase"
    assert route_after_slots(_state(cli)) == "policy"
    charge = _state(cli, {"market": "IN", "product_family": "personal_charge"})
    assert route_after_slots(charge) == "fail_closed"


# --------------------------------------------------------------------------- #
# The emergency gate runs before any classifier
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("message,expected", [
    ("Someone is forcing me to transfer money right now",
     "emergency_security.under_duress_or_coercion"),
    ("My father passed away last week", "bereavement_legal.notify_cardmember_death"),
    ("I see a purchase I never made", "fraud_security.unrecognized_transaction"),
    ("I lost my job and can't pay", "hardship_collections.cannot_pay"),
])
def test_emergency_phrases_bypass_classification(message, expected):
    assert catalogue.match_emergency(message) == expected


def test_ordinary_request_does_not_trip_the_emergency_gate():
    assert catalogue.match_emergency("please waive my late fee") is None
    assert catalogue.match_emergency("what is my balance") is None


def test_emergency_operations_are_top_priority():
    assert catalogue.priority_of("emergency_security.under_duress_or_coercion") == 1
    assert catalogue.priority_of("fraud_security.unrecognized_transaction") == 2
    assert (catalogue.priority_of("fees_interest.request_fee_waiver")
            > catalogue.priority_of("hardship_collections.cannot_pay"))
