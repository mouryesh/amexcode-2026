"""backend/policy_registry.py — intent to policy. Dispatch, not retrieval.

A static map loaded with the policy bundle. No database, no search, no network:
if semantic matching picked the policy, a near miss could apply the fee-waiver
rule to a credit-limit request and you could never prove afterwards which rule
should have fired. Exact lookup can only hit or miss, and a miss is fail-closed.

Key is (domain.operation, market, product_family) because the same intent takes
a different policy on a charge card, or outside India.

Imports: shared.exceptions.
"""
from __future__ import annotations

from typing import Optional

from shared.exceptions import PolicyNotBound

# (intent, market, product_family) -> policy_id
# product_family "*" matches anything not listed explicitly.
_REGISTRY: dict[tuple[str, str, str], str] = {
    ("fees_interest.request_fee_waiver", "IN", "*"): "fee.late.courtesy_waiver",
    ("fees_interest.request_fee_reversal", "IN", "*"): "fee.late.courtesy_waiver",
    ("credit_spending_power.permanent_credit_limit_increase", "IN", "personal_credit"):
        "credit.limit_increase",
    ("card_lifecycle_controls.replace_damaged_card", "IN", "*"): "card.replacement",
    ("card_lifecycle_controls.report_lost_card", "IN", "*"): "card.replacement",
    ("card_lifecycle_controls.replace_not_received_card", "IN", "*"): "card.replacement",
}


def resolve(intent: str, market: str = "IN", product_family: str = "personal_credit") -> str:
    """Return the policy_id for an intent, or raise PolicyNotBound.

    The raise is the fail-closed path: no approved policy for a consequential
    request means hand off, never infer approval.
    """
    for key in ((intent, market, product_family), (intent, market, "*")):
        if key in _REGISTRY:
            return _REGISTRY[key]
    raise PolicyNotBound(
        f"No approved policy for intent '{intent}' "
        f"(market={market}, product_family={product_family})."
    )


def try_resolve(intent: str, market: str = "IN",
                product_family: str = "personal_credit") -> Optional[str]:
    """Non-raising variant, for the read-only branch that may answer from source."""
    try:
        return resolve(intent, market, product_family)
    except PolicyNotBound:
        return None


def bound_intents() -> list[str]:
    """Every intent with an approved policy. Everything else fails closed."""
    return sorted({k[0] for k in _REGISTRY})
