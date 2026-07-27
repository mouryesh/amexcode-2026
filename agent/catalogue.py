"""agent/catalogue.py — the closed-world routing catalogue, loaded from spec.

Every operation the agent can recognise lives in `specs/amex_in_catalog.yaml`,
not in Python. This module loads it once and answers the four questions the
graph asks of any classified request:

    resolve(op)          is this a real catalogue operation?
    is_consequential(op) may it execute, or must it only inform?
    profile_of(op)       which ST-* profile scopes the account read?
    priority_of(op)      does it pre-empt everything else this turn?

Why data and not code: the catalogue has 380 operations across 27 domains and
only a handful are automated today. Encoding it as a dict-of-dicts in Python
would mean every new operation is a code change and a deploy. As data, adding
one is a line of YAML — and the fail-closed path below means an operation with
no policy is *already* handled safely the moment it is listed.

The distinction that makes that safe is `read_only`:

    read_only      + no policy  ->  inform from an approved source (INFORM)
    consequential  + no policy  ->  do NOT execute, build a handoff (UNMAPPED)

That is POL-GLOBAL-011 verbatim, and it is what makes all 380 operations
deterministic while only six of them are automated.

Imports: shared.config.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

_SPEC = Path(__file__).resolve().parent.parent / "specs" / "amex_in_catalog.yaml"

# Domains without an explicit tier sit at "all_other_service_intents".
_DEFAULT_TIER = 7


@dataclass(frozen=True)
class Operation:
    """One catalogue leaf, fully qualified as `domain.operation`."""

    domain: str
    operation: str
    read_only: bool
    state_profile: str
    flow: str
    default_mode: str
    priority_tier: int

    @property
    def id(self) -> str:
        return f"{self.domain}.{self.operation}"

    @property
    def consequential(self) -> bool:
        return not self.read_only


@dataclass(frozen=True)
class EmergencyGate:
    """A deterministic pre-classification match (ordered_transition_engine #2)."""

    operation_id: str
    phrases: tuple[str, ...]


def _load() -> dict:
    with _SPEC.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=1)
def _catalogue() -> tuple[dict[str, Operation], dict[str, dict], tuple[EmergencyGate, ...]]:
    raw = _load()
    ops: dict[str, Operation] = {}
    domains: dict[str, dict] = {}

    for domain, body in raw["domains"].items():
        domains[domain] = {
            "description": body.get("description", ""),
            "state_profile": body["state_profile"],
            "flow": body["flow"],
            "default_mode": body["default_mode"],
            "priority_tier": body.get("priority_tier", _DEFAULT_TIER),
            "operations": [o["name"] for o in body["operations"]],
        }
        for entry in body["operations"]:
            op = Operation(
                domain=domain,
                operation=entry["name"],
                read_only=bool(entry["read_only"]),
                state_profile=body["state_profile"],
                flow=body["flow"],
                default_mode=body["default_mode"],
                priority_tier=body.get("priority_tier", _DEFAULT_TIER),
            )
            ops[op.id] = op

    gates = tuple(
        EmergencyGate(f"{g['domain']}.{g['operation']}", tuple(g["phrases"]))
        for g in raw.get("emergency_modifiers", {}).values()
    )
    return ops, domains, gates


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #
def operations() -> dict[str, Operation]:
    """Every catalogue operation, keyed `domain.operation`."""
    return _catalogue()[0]


def domains() -> dict[str, dict]:
    return _catalogue()[1]


def resolve(operation_id: str) -> Optional[Operation]:
    """The Operation for `domain.operation`, or None if it is not in the catalogue."""
    return operations().get(operation_id)


def exists(operation_id: str) -> bool:
    return operation_id in operations()


def is_consequential(operation_id: str) -> bool:
    """True when the operation may change an account and so must not execute
    without an approved policy. Unknown ids are consequential — fail safe."""
    op = resolve(operation_id)
    return True if op is None else op.consequential


def profile_of(operation_id: str) -> Optional[str]:
    op = resolve(operation_id)
    return op.state_profile if op else None


def flow_of(operation_id: str) -> Optional[str]:
    op = resolve(operation_id)
    return op.flow if op else None


def priority_of(operation_id: str) -> int:
    """Lower is more urgent. Unknown operations sort last."""
    op = resolve(operation_id)
    return op.priority_tier if op else _DEFAULT_TIER + 1


def operations_in(domain: str) -> list[str]:
    return domains().get(domain, {}).get("operations", [])


# --------------------------------------------------------------------------- #
# Emergency gate — runs BEFORE classification
# --------------------------------------------------------------------------- #
def match_emergency(message: str) -> Optional[str]:
    """The operation id for a duress/fraud/bereavement/hardship phrase, else None.

    Deterministic substring matching, deliberately: the highest-priority
    operations in the catalogue must never depend on a classifier's confidence.
    Gates are checked in spec order, and the spec lists them by tier, so duress
    is tested before fraud and fraud before hardship.
    """
    if not message:
        return None
    lowered = message.lower()
    for gate in _catalogue()[2]:
        if any(phrase in lowered for phrase in gate.phrases):
            return gate.operation_id
    return None


# --------------------------------------------------------------------------- #
# Offline lexical scoring — the no-LLM fallback across all 380
# --------------------------------------------------------------------------- #
_STOP = frozenset({
    "a", "an", "the", "my", "me", "i", "is", "are", "was", "to", "for", "of", "on",
    "in", "it", "this", "that", "and", "or", "can", "you", "please", "want", "need",
    "would", "like", "do", "does", "did", "have", "has", "get", "got", "am", "be",
    "with", "from", "card", "amex",
})


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t not in _STOP}


@lru_cache(maxsize=1)
def _op_tokens() -> list[tuple[str, frozenset[str]]]:
    """Token set per operation, from its own id. Built once."""
    out = []
    for op_id, op in operations().items():
        toks = _tokens(op.operation) | _tokens(op.domain)
        out.append((op_id, frozenset(toks)))
    return out


def score_lexical(message: str, top_k: int = 5) -> list[tuple[str, float]]:
    """Rank catalogue operations by token overlap with the message.

    A weak signal on its own — operation ids are machine names, not phrasings —
    so it is used only as the offline fallback and its scores are deliberately
    reported low so the confidence gate treats them as uncertain.
    """
    msg = _tokens(message)
    if not msg:
        return []
    scored = []
    for op_id, toks in _op_tokens():
        if not toks:
            continue
        overlap = len(msg & toks)
        if overlap:
            scored.append((op_id, overlap / (len(toks) ** 0.5)))
    scored.sort(key=lambda kv: -kv[1])
    if not scored:
        return []
    top = scored[0][1]
    return [(op_id, round(s / top * 0.6, 4)) for op_id, s in scored[:top_k]]
