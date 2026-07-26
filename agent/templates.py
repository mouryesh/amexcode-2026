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
}


def render(template_id: str, **kwargs: str) -> str:
    """Fill an approved template's placeholders with already-known values.

    Raises KeyError if the template id is unknown — a typo here should fail
    loudly, not silently fall through to some other text.
    """
    return TEMPLATES[template_id].format(**kwargs)
