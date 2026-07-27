"""agent/templates.py — approved, fixed response text, keyed by template id.

Per the reference policy design this system is built against: the LLM may
explain a result only through an approved template. It cannot change a policy
result or invent eligibility. These strings are the actual output for
regulated/high-stakes turns — Python fills placeholders with values already
computed elsewhere (decision.reason_text, account facts). No LLM call happens
to produce this text; `render()` is plain string formatting.

Naming follows the T-* convention from that reference design so the two stay
easy to cross-reference.

Imports: stdlib only.
"""
from __future__ import annotations

TEMPLATES: dict[str, str] = {
    "T-SECRET-REDACTED": (
        "For your security, please don't send your PIN, password, OTP, CVV or "
        "full card number in chat. I won't use or repeat that value — it has "
        "been redacted. Please use the secure app or website whenever a "
        "confidential value is needed."
    ),
    "T-PROMPT-INJECTION": (
        "I can't bypass servicing policy, reveal private system information, "
        "or access another customer's data. I can still help with a "
        "legitimate card servicing request."
    ),
    "T-SELECT-ITEM": (
        "I found more than one match:\n{options}\nPlease reply with the "
        "option number."
    ),
    # The fail-closed pair. A recognised catalogue operation with no approved
    # policy takes one of these two, decided by read_only in the catalogue —
    # never by the model, and never by falling through to a tool.
    "T-UNMAPPED": (
        "That is an American Express servicing request, but it isn't one of my "
        "approved automated flows. I won't guess at it or make any change to "
        "your account. I've passed the full context to a specialist so you "
        "won't need to start again."
    ),
    "T-INFORM-NO-SOURCE": (
        "You're asking about {topic}. I don't have a current approved source "
        "for that detail, so I won't guess — the terms differ by card and I'd "
        "rather give you nothing than give you something wrong. The official "
        "Amex India page or Customer Care will have the current answer."
    ),
    "T-LOOP-STOP": (
        "We're going round the same step without getting what's needed to "
        "proceed, and I haven't made any change to your account. I can connect "
        "you with a specialist who has the context so far, or close this here."
    ),
}


def render(template_id: str, **kwargs: str) -> str:
    """Fill an approved template's placeholders with already-known values.

    Raises KeyError if the template id is unknown — a typo here should fail
    loudly, not silently fall through to some other text.
    """
    return TEMPLATES[template_id].format(**kwargs)
