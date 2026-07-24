"""tests/test_graph.py — end-to-end integration across the four personas.

Drives the whole agent pipeline (classify → slots → policy → tool → respond)
through the graph and asserts each persona's scored outcome. Runs on a temp DB
(conftest) in offline LLM mode.
"""
import pytest

from agent.graph import run
from agent.state import new_state
from backend import ledger
from backend.seed import seed
from shared.schemas import Outcome


@pytest.fixture(autouse=True)
def _seeded():
    seed()


def _run(member, message, **kw):
    return run(new_state(f"t-{member}", member, message, **kw))


def test_priya_fee_waiver_approves_and_writes():
    s = _run("MEM-PRIYA", "please waive my late fee")
    assert s["intent"].label == "fee_waiver"
    assert s["decision_records"][-1].outcome == Outcome.APPROVE
    assert any(a["tool"] == "reverse_fee" and a["status"] == "ok"
               for a in s["actions_taken"])
    assert not s["escalate"]


def test_rahul_fee_waiver_declines_no_write():
    s = _run("MEM-RAHUL", "can you waive my late fee")
    assert s["decision_records"][-1].outcome == Outcome.DECLINE
    assert s["decision_records"][-1].reason_code == "WAIVER_FREQUENCY_EXCEEDED"
    # A decline performs no write action.
    assert all(a.get("status") != "ok" for a in s.get("actions_taken", []))


def test_ananya_limit_increase_queues():
    s = _run("MEM-ANANYA", "I want to raise my credit limit")
    assert s["decision_records"][-1].outcome == Outcome.QUEUE
    assert any(a["tool"] == "open_underwriting_case" for a in s["actions_taken"])


def test_vikram_hardship_escalates_and_suppresses_collections():
    s = _run("MEM-VIKRAM", "I lost my job and can't pay this month")
    assert s["escalate"] is True
    packet = s["escalation_packet"]
    assert packet is not None
    assert "HARDSHIP" in packet.reason
    assert packet.member_sentiment in ("distressed", "negative")
    # Collections suppression flag was set as part of the handoff.
    assert any(a["tool"] == "flag_vulnerability" for a in s["actions_taken"])


def test_propose_confirm_requires_confirmation():
    without = _run("MEM-PRIYA", "my card is damaged, send a replacement")
    assert without["awaiting_member"] is True
    assert not any(a.get("status") == "ok" for a in without["actions_taken"])

    withc = _run("MEM-PRIYA", "my card is damaged, send a replacement", confirm=True)
    assert any(a["tool"] == "issue_replacement" and a["status"] == "ok"
               for a in withc["actions_taken"])


def test_step_up_requires_reauthentication():
    without = _run("MEM-PRIYA", "reset my pin")
    assert without["awaiting_member"] is True
    withr = _run("MEM-PRIYA", "reset my pin", reauthenticated=True)
    assert any(a["tool"] == "reset_pin" and a["status"] == "ok"
               for a in withr["actions_taken"])


def test_low_confidence_asks_to_clarify():
    s = _run("MEM-PRIYA", "hello there")
    assert s["intent"].label == "clarify"
    assert s["awaiting_member"] is True


def test_pipeline_writes_are_ledgered_and_verify():
    _run("MEM-PRIYA", "please waive my late fee")
    assert ledger.verify()["status"] == "OK"
    records = ledger.records_for_session("t-MEM-PRIYA")
    assert any(r["action"] == "reverse_fee" for r in records)
