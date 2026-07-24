"""tests/test_policy_engine.py — one test per branch, parametrized.

The policy engine is a pure function; these tests are its proof. Adding a new
case is one line.
"""
import pytest

from agent.policy_engine import evaluate
from shared.exceptions import InsufficientData, PolicyNotFound
from shared.schemas import Outcome


def _fee_facts(**overrides):
    facts = {
        "tenure_months": 36,
        "late_payments_12m": 0,
        "prior_waivers_8m": 0,
        "distress_signals": False,
    }
    facts.update(overrides)
    return facts


# --------------------------- fee courtesy waiver --------------------------- #
@pytest.mark.parametrize(
    "facts, expected, reason_code",
    [
        # Priya: clean payer, long tenure, no prior waivers.
        (_fee_facts(), Outcome.APPROVE, "GOODWILL_WAIVER_GRANTED"),
        # Rahul: two waivers in the window.
        (_fee_facts(prior_waivers_8m=2), Outcome.DECLINE, "WAIVER_FREQUENCY_EXCEEDED"),
        # New account under minimum tenure.
        (_fee_facts(tenure_months=3), Outcome.DECLINE, "MIN_TENURE_NOT_MET"),
        # Distress overrides everything.
        (_fee_facts(distress_signals=True), Outcome.ESCALATE, "HARDSHIP_SIGNAL"),
        # Exact tenure boundary (6 months) still approves.
        (_fee_facts(tenure_months=6), Outcome.APPROVE, "GOODWILL_WAIVER_GRANTED"),
        # One late payment still within tolerance.
        (_fee_facts(late_payments_12m=1), Outcome.APPROVE, "GOODWILL_WAIVER_GRANTED"),
    ],
)
def test_fee_waiver_branches(facts, expected, reason_code):
    decision = evaluate("fee.late.courtesy_waiver", facts)
    assert decision.outcome == expected
    assert decision.reason_code == reason_code
    assert decision.policy_version == "v1"
    # Every fact the winning rule read is recorded for the audit trail.
    assert isinstance(decision.inputs_used, dict)


def test_approve_clean_payer_is_priya():
    d = evaluate("fee.late.courtesy_waiver", _fee_facts())
    assert d.outcome == Outcome.APPROVE
    assert d.rule_id == "clean_payer_approve"


# --------------------------- credit limit increase ------------------------- #
def _credit_facts(**overrides):
    facts = {
        "tenure_months": 24,
        "income_staleness_months": 3,
        "utilisation": 0.4,
        "late_payments_12m": 0,
        "distress_signals": False,
    }
    facts.update(overrides)
    return facts


@pytest.mark.parametrize(
    "facts, expected, reason_code",
    [
        # Ananya: stale income → queue.
        (_credit_facts(income_staleness_months=14, tenure_months=6),
         Outcome.QUEUE, "INCOME_DATA_STALE"),
        (_credit_facts(), Outcome.APPROVE, "LIMIT_INCREASE_GRANTED"),
        (_credit_facts(utilisation=0.95), Outcome.DECLINE, "HIGH_UTILISATION"),
        (_credit_facts(tenure_months=8), Outcome.QUEUE, "THIN_FILE"),
        (_credit_facts(distress_signals=True), Outcome.ESCALATE, "HARDSHIP_SIGNAL"),
    ],
)
def test_credit_branches(facts, expected, reason_code):
    d = evaluate("credit.limit_increase", facts)
    assert d.outcome == expected
    assert d.reason_code == reason_code


# --------------------------- error handling -------------------------------- #
def test_unknown_policy_id_raises():
    with pytest.raises(PolicyNotFound):
        evaluate("does.not.exist", {})


def test_missing_fact_raises_insufficient_data():
    with pytest.raises(InsufficientData):
        evaluate("fee.late.courtesy_waiver", {"tenure_months": 36})  # missing others
