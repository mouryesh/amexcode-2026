"""tests/test_sessions.py — backend.sessions round-trip and repeat-decline counting."""
from backend import sessions
from backend.database import db_session, init_db


def _fresh():
    init_db()
    with db_session() as conn:
        conn.execute("DELETE FROM messages")


def test_record_and_get_history_round_trips_in_order():
    _fresh()
    sessions.record_turn("s1", "MEM-X", "member", "hello")
    sessions.record_turn("s1", "MEM-X", "agent", "hi there")
    sessions.record_turn("s1", "MEM-X", "member", "waive my fee")

    history = sessions.get_history("s1")
    assert history == [
        {"role": "member", "text": "hello"},
        {"role": "agent", "text": "hi there"},
        {"role": "member", "text": "waive my fee"},
    ]


def test_get_history_scoped_to_session():
    _fresh()
    sessions.record_turn("s1", "MEM-X", "member", "for session one")
    sessions.record_turn("s2", "MEM-X", "member", "for session two")

    assert sessions.get_history("s1") == [{"role": "member", "text": "for session one"}]
    assert sessions.get_history("s2") == [{"role": "member", "text": "for session two"}]


def test_count_prior_declines_scoped_to_policy_and_reason():
    _fresh()
    sessions.record_turn("s1", "MEM-X", "agent", "declined once",
                         decision_outcome="DECLINE",
                         decision_reason_code="WAIVER_FREQUENCY_EXCEEDED",
                         policy_id="fee.late.courtesy_waiver")
    assert sessions.count_prior_declines(
        "s1", "fee.late.courtesy_waiver", "WAIVER_FREQUENCY_EXCEEDED"
    ) == 1
    # A different reason code on the same policy doesn't count.
    assert sessions.count_prior_declines(
        "s1", "fee.late.courtesy_waiver", "MIN_TENURE_NOT_MET"
    ) == 0
    # A different session doesn't count.
    assert sessions.count_prior_declines(
        "s2", "fee.late.courtesy_waiver", "WAIVER_FREQUENCY_EXCEEDED"
    ) == 0


def test_record_turn_redacts_pii_before_storage():
    _fresh()
    sessions.record_turn("s1", "MEM-X", "member",
                         "my card is 4111111111111111 and email is a@b.com")
    stored = sessions.get_history("s1")[0]["text"]
    assert "4111111111111111" not in stored
    assert "a@b.com" not in stored
    assert "[REDACTED_CARD_NUMBER]" in stored
    assert "[REDACTED_EMAIL]" in stored


def test_record_turn_preserves_intent_bearing_text():
    _fresh()
    sessions.record_turn("s1", "MEM-X", "member", "please waive my late fee")
    assert sessions.get_history("s1")[0]["text"] == "please waive my late fee"
