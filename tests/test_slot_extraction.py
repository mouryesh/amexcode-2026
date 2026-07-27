"""tests/test_slot_extraction.py — the multi-turn slot fill loop.

Covers the path a member actually takes when they answer a question in stages:
state the request, get asked for what's missing, supply it in a bare reply.

The extractor itself calls the LLM, so these tests stub `llm` rather than hit a
provider — the assertions are about the graph's behaviour around the model
(what it asks for, what it refuses to accept, when it stops asking), not about
the model's own accuracy. The offline no-op path is asserted directly.
"""
import pytest

from agent import nodes
from agent.graph import route_after_slots, run
from agent.state import new_state
from backend import sessions
from backend.seed import seed
from shared.config import config


@pytest.fixture(autouse=True)
def _seeded():
    seed()


class _StubLLM:
    """Stands in for agent.llm.llm — records the prompt, returns a fixed dict.

    `payload` may be a callable taking the rendered user prompt, so a test can
    model a real extractor: report the value only when the member actually
    said it.
    """

    def __init__(self, payload, available=True):
        self.payload, self._available, self.last_user = payload, available, None

    def available(self):
        return self._available

    def structured_json(self, system, user, schema_hint):
        self.last_user = user
        return self.payload(user) if callable(self.payload) else self.payload


@pytest.fixture
def stub(monkeypatch):
    def _install(payload, available=True):
        s = _StubLLM(payload, available)
        monkeypatch.setattr(nodes, "llm", s)
        return s
    return _install


def _to_slots(session_id, member, message, **kw):
    """Drive the pipeline up to and including extraction."""
    st = new_state(session_id, member, message,
                   history=sessions.get_history(session_id), **kw)
    st = nodes.security_filter(st)
    st = nodes.classify_intent(st)
    st = nodes.check_slots(st)
    return nodes.extract_slots(st)


# --------------------------------------------------------------------------- #
# The loop closes
# --------------------------------------------------------------------------- #
def test_bare_reply_fills_the_slot_and_completes(stub):
    ADDR = "42 Brigade Road, Bengaluru 560001"
    # Only report the address once the member has actually stated it — turn 1
    # names the request, turn 2 supplies the value.
    stub(lambda user: {"address": {"value": ADDR, "confidence": 0.95}}
         if ADDR in user else {"address": {"value": None, "confidence": 0.0}})
    sid = "slot-fill"

    first = _to_slots(sid, "MEM-PRIYA", "I need to change my address")
    assert first["missing_slots"] == ["address"]
    assert not first["slots_complete"]
    assert route_after_slots(first) == "ask"

    sessions.record_turn(sid, "MEM-PRIYA", "member", "I need to change my address")
    second = _to_slots(sid, "MEM-PRIYA", ADDR)

    # A bare address carries no intent signal; the open flow supplies it.
    assert second["intent"].label == "update_address"
    assert second["slots"]["address"] == "42 Brigade Road, Bengaluru 560001"
    assert second["slots_complete"]
    assert route_after_slots(second) == "direct_tool"


def test_extracted_value_is_persisted_with_provenance(stub):
    stub({"address": {"value": "9 MG Road, Pune 411001", "confidence": 0.88}})
    sid = "slot-prov"
    _to_slots(sid, "MEM-PRIYA", "change my address")

    stored = sessions.get_slots(sid)["address"]
    assert stored["value"] == "9 MG Road, Pune 411001"
    assert stored["source"] == "llm_extraction"
    assert stored["confidence"] == 0.88
    # Classified, not defaulted — an unclassified slot would be 'restricted'
    # and would be purged on terminalize.
    assert stored["sensitivity"] == "internal"


# --------------------------------------------------------------------------- #
# The three guards
# --------------------------------------------------------------------------- #
def test_low_confidence_value_is_discarded_and_reasked(stub):
    below = config.SLOT_EXTRACT_MIN_CONFIDENCE - 0.1
    stub({"address": {"value": "somewhere in Bengaluru", "confidence": below}})

    st = _to_slots("slot-lowconf", "MEM-PRIYA", "change my address")
    assert st["slots"].get("address") is None
    assert st["missing_slots"] == ["address"]
    assert route_after_slots(st) == "ask"


def test_restricted_slot_is_never_offered_to_the_model(stub, monkeypatch):
    """A secret must reach the auth backend, never a chat turn.

    security_filter already blocks such a message; this asserts the second
    lock — even reached directly, no model output can populate a restricted
    slot, because the slot is never named in the prompt.
    """
    monkeypatch.setitem(nodes.MEMBER_SLOTS, "update_address", ("address", "otp"))
    s = stub({"address": {"value": "1 Residency Rd", "confidence": 0.99},
              "otp": {"value": "998877", "confidence": 0.99}})

    st = _to_slots("slot-secret", "MEM-PRIYA", "change my address")

    assert "otp" not in s.last_user, "a restricted slot name reached the prompt"
    assert "otp" not in st["slots"]
    assert "otp" in st["missing_slots"]
    assert "otp" not in sessions.get_slots("slot-secret")


def test_offline_extraction_is_a_no_op(stub):
    s = stub({"address": {"value": "should not be used", "confidence": 1.0}},
             available=False)
    st = _to_slots("slot-offline", "MEM-PRIYA", "change my address")

    assert s.last_user is None, "no LLM call should be attempted offline"
    assert st["slots"].get("address") is None
    assert not st["slots_complete"]


# --------------------------------------------------------------------------- #
# The loop is bounded
# --------------------------------------------------------------------------- #
def test_unanswered_slot_escalates_at_the_attempt_limit(stub):
    stub({"address": {"value": None, "confidence": 0.0}})
    sid = "slot-bound"

    for _ in range(config.SLOT_MAX_ATTEMPTS - 1):
        st = _to_slots(sid, "MEM-PRIYA", "I need to change my address")
        assert route_after_slots(st) == "ask"
        assert not st.get("slots_exhausted")

    final = _to_slots(sid, "MEM-PRIYA", "I need to change my address")
    assert final["slots_exhausted"]
    assert route_after_slots(final) == "escalate"


def test_hardship_wins_over_an_open_slot_flow(stub):
    """An open flow restores intent only for 'clarify'. Distress is not that."""
    stub({"address": {"value": None, "confidence": 0.0}})
    sid = "slot-hardship"
    _to_slots(sid, "MEM-VIKRAM", "I want to change my address")
    assert sessions.get_active_flow(sid)["operation"] == "update_address"

    s = run(new_state(sid, "MEM-VIKRAM", "I lost my job and can't pay",
                      history=sessions.get_history(sid)))
    assert s["intent"].label == "hardship"
    assert s["escalate"]


# --------------------------------------------------------------------------- #
# Existing behaviour is untouched
# --------------------------------------------------------------------------- #
def test_disambiguation_still_bypasses_extraction(stub):
    """Two candidates is a 'pick one' question, not a missing value."""
    s = stub({"txn_ref": {"value": "TXN-DEEPA-FEE-1", "confidence": 0.99}})
    st = _to_slots("slot-ambig", "MEM-DEEPA", "waive my late fee")

    assert st["ambiguous_field"] == "txn_ref"
    assert s.last_user is None, "extraction must not run over an ambiguous field"
    assert route_after_slots(st) == "ask"
