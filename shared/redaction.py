"""shared/redaction.py — strip PII-shaped substrings before text is persisted.

Applied at the boundary where raw member/agent text is about to be stored
(backend/sessions.py) or handed to a human (agent/escalation.py's transcript)
— matching the "anonymize before it's stored/exposed" pattern real bank
chatbot deployments use (see the on-premises banking-chatbot reference
architecture reviewed for this project: the German Federal Employment
Agency's "Digitaler Lotse" anonymises interaction data by removing user-
identifiable text before storage).

Does NOT affect the current turn's live processing — check_slots and tools.py
always operate on the in-memory message, never the redacted stored copy. This
only protects data AT REST and in the escalation handoff, not the live turn.

Pattern-based, not full NER — appropriate scope for this system. Catches the
shapes that matter most: card/account numbers, emails, phone numbers, and
other long ID-like digit sequences. Deliberately does NOT touch structured
server-side facts (account_facts) — those are numbers a human reviewer
legitimately needs (tenure, waiver counts); this only redacts free text a
member typed that might accidentally contain a real card/account number.

Imports: stdlib only.
"""
from __future__ import annotations

import re

# Order matters: email first, so digits inside an email's local part are
# consumed before the digit-oriented patterns below get a chance at them.
_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    # (?=\d) lookahead: only consume a separator when another digit follows,
    # so a trailing space/dash after the LAST digit is left alone instead of
    # being greedily eaten (which used to produce "[REDACTED_...]and email").
    ("CARD_NUMBER", re.compile(r"\b(?:\d(?:[ -](?=\d))?){13,19}\b")),
    # \b anchors matter here: without them this pattern could match a chunk out
    # of the MIDDLE of a longer digit run (e.g. the first 16 of a 20-digit id),
    # leaving a mangled remainder instead of redacting cleanly.
    ("PHONE", re.compile(r"\b\+?\d[\d\-\s]{8,14}\d\b")),
    ("LONG_ID", re.compile(r"\b\d{9,}\b")),
]


def redact(text: str) -> str:
    """Replace PII-shaped substrings with a [REDACTED_<TYPE>] placeholder.

    Idempotent — safe to call on already-redacted text, since the placeholders
    themselves don't match any pattern (no digit runs, no @ sign). Leaves
    everything else untouched, including intent-carrying words ("waive",
    "hardship") and short numbers (amounts, tenure) — so the redacted text is
    still useful for grounding LLM prose and sentiment detection.
    """
    if not text:
        return text
    redacted = text
    for label, pattern in _PATTERNS:
        redacted = pattern.sub(f"[REDACTED_{label}]", redacted)
    return redacted
