"""tests/test_escalation.py — escalation packet redacts PII from the transcript
before handing it to a human, while keeping structured facts and sentiment intact."""
from agent.escalation import build_escalation_packet


def test_transcript_is_redacted():
    messages = [
        {"role": "member", "text": "my card number is 4111111111111111"},
        {"role": "agent", "text": "I understand this is important."},
    ]
    packet = build_escalation_packet(
        session_id="s1", member_id="MEM-VIKRAM", reason="HARDSHIP_SIGNAL: test",
        messages=messages, facts_read={"tenure_months": 48}, actions_attempted=[],
        decisions=[],
    )
    assert "4111111111111111" not in str(packet.transcript)
    assert any("[REDACTED_CARD_NUMBER]" in m["text"] for m in packet.transcript)


def test_sentiment_keywords_survive_redaction():
    # Redaction must not eat the words sentiment detection depends on.
    messages = [{"role": "member", "text": "I lost my job and can't pay this month"}]
    packet = build_escalation_packet(
        session_id="s1", member_id="MEM-VIKRAM", reason="HARDSHIP_SIGNAL: test",
        messages=messages, facts_read={}, actions_attempted=[], decisions=[],
    )
    assert packet.member_sentiment == "distressed"
    assert packet.transcript[0]["text"] == "I lost my job and can't pay this month"


def test_structured_facts_are_not_redacted():
    # facts_read is server-side structured data, not free text — must pass through untouched.
    facts = {"tenure_months": 48, "member_id": "MEM-VIKRAM", "utilisation": 0.88}
    packet = build_escalation_packet(
        session_id="s1", member_id="MEM-VIKRAM", reason="HARDSHIP_SIGNAL: test",
        messages=[], facts_read=facts, actions_attempted=[], decisions=[],
    )
    assert packet.facts_read == facts
