"""tests/test_redaction.py — PII-shaped substrings get stripped before storage/handoff."""
from shared.redaction import redact


def test_card_number_redacted():
    assert redact("my card number is 4111111111111111") == \
        "my card number is [REDACTED_CARD_NUMBER]"


def test_spaced_card_number_redacted():
    assert "[REDACTED_CARD_NUMBER]" in redact("card: 4111 1111 1111 1111 please")


def test_email_redacted():
    assert redact("reach me at priya.sharma@example.com") == \
        "reach me at [REDACTED_EMAIL]"


def test_phone_number_redacted():
    assert "[REDACTED_PHONE]" in redact("call me on +91-90000-00001 anytime")


def test_long_id_redacted():
    # A 12-digit ID may be caught by PHONE or LONG_ID depending on overlap —
    # either is correct redaction; what matters is the raw number never survives.
    result = redact("my aadhaar is 123456789012")
    assert "123456789012" not in result
    assert result.startswith("my aadhaar is [REDACTED_")


def test_standalone_long_id_not_shaped_like_phone():
    # A 20-digit run has no phone-length match (>16 total), falls through to LONG_ID.
    result = redact("reference number 12345678901234567890 on file")
    assert "12345678901234567890" not in result
    assert "[REDACTED_LONG_ID]" in result


def test_intent_bearing_text_untouched():
    # The whole point: redaction must never eat the words that carry meaning.
    text = "I can't pay and I want my late fee waived, I lost my job"
    assert redact(text) == text


def test_short_amounts_and_tenure_untouched():
    # Amounts/tenure are short numbers, not PII-shaped — must survive.
    text = "I've been a member for 36 months and was charged 500 rupees"
    assert redact(text) == text


def test_idempotent_on_already_redacted_text():
    once = redact("card 4111111111111111 email a@b.com")
    twice = redact(once)
    assert once == twice


def test_empty_and_none_safe():
    assert redact("") == ""


def test_multiple_pii_types_in_one_message():
    text = "my email is a@b.com and card is 4111111111111111"
    result = redact(text)
    assert "[REDACTED_EMAIL]" in result
    assert "[REDACTED_CARD_NUMBER]" in result
    assert "a@b.com" not in result
    assert "4111111111111111" not in result
