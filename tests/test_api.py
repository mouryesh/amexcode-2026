"""tests/test_api.py — end-to-end tests through the real HTTP endpoints.

Uses TestClient as a context manager so FastAPI's startup hook (init_db + seed)
actually runs — a bare TestClient(app) call skips lifespan events.
"""
from fastapi.testclient import TestClient

from api.main import app


def test_repeated_decline_escalates():
    """Rahul declines normally once; asking again for the same reason escalates.

    This is the resolution to the "persistent request" open question: the
    policy decision itself never changes (pure function of account facts), but
    the SESSION PATTERN of repeating an identical decline now triggers the
    appeal path the decline text already promises, instead of looping forever.
    """
    with TestClient(app) as c:
        session_id = "repeat-test-rahul"
        payload = {
            "session_id": session_id,
            "member_id": "MEM-RAHUL",
            "message": "please waive my late fee",
        }

        first = c.post("/agent/message", json=payload).json()
        assert first["decision_records"][0]["outcome"] == "DECLINE"
        assert first["decision_records"][0]["reason_code"] == "WAIVER_FREQUENCY_EXCEEDED"
        assert first["escalate"] is False

        second = c.post("/agent/message", json=payload).json()
        assert second["decision_records"][0]["outcome"] == "ESCALATE"
        assert second["decision_records"][0]["reason_code"] == "REPEATED_DECLINE_ESCALATED"
        assert second["escalate"] is True
        assert second["escalation_packet"] is not None
        assert "REPEATED_DECLINE_ESCALATED" in second["escalation_packet"]["reason"]

        # The escalation packet's transcript now carries the whole conversation,
        # not just the current message — both prior turns plus this one.
        transcript = second["escalation_packet"]["transcript"]
        assert len(transcript) == 3  # member ask 1, agent decline 1, member ask 2


def test_unrelated_requests_dont_count_toward_repeat_threshold():
    """A decline on one policy must not push a different policy's count."""
    with TestClient(app) as c:
        session_id = "repeat-test-independent"
        c.post("/agent/message", json={
            "session_id": session_id, "member_id": "MEM-RAHUL",
            "message": "please waive my late fee",
        })
        # A different request in the same session — must not be affected.
        info = c.post("/agent/message", json={
            "session_id": session_id, "member_id": "MEM-RAHUL",
            "message": "what's my current balance?",
        }).json()
        assert info["escalate"] is False


def test_priya_single_ask_still_approves_normally():
    """Sanity check: the repeat-escalation logic must not affect a fresh approve."""
    with TestClient(app) as c:
        result = c.post("/agent/message", json={
            "session_id": "repeat-test-priya", "member_id": "MEM-PRIYA",
            "message": "please waive my late fee",
        }).json()
        assert result["decision_records"][0]["outcome"] == "APPROVE"
        assert result["escalate"] is False


def test_history_persists_across_turns():
    with TestClient(app) as c:
        session_id = "history-test"
        c.post("/agent/message", json={
            "session_id": session_id, "member_id": "MEM-PRIYA", "message": "hello",
        })
        c.post("/agent/message", json={
            "session_id": session_id, "member_id": "MEM-PRIYA", "message": "hello again",
        })
        records = c.get(f"/audit/{session_id}").json()
        # Doesn't assert on ledger content directly (no writes here) — this just
        # exercises the same session_id across two calls without erroring.
        assert records["session_id"] == session_id
