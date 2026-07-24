"""agent/policy_engine.py — the crown jewel.

    evaluate(policy_id, account_facts) -> Decision

A pure function with ZERO upward dependencies. Loads a versioned YAML policy
from /policies, runs deterministic rules against account facts, and returns a
self-explaining Decision recording every input it used.

This is the answer to "how do you know the model didn't hallucinate an
approval?" — the model never sees these thresholds. The decision is a pure
function of account facts and a versioned document.

Imports ONLY: shared.schemas, shared.config, shared.exceptions, yaml.
No LLM. No database. No network. No side effects.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

from shared.config import config
from shared.exceptions import InsufficientData, PolicyNotFound
from shared.schemas import Decision, Outcome

# Comparison operators the YAML `op` field may reference. Kept tiny and total —
# an unknown operator is a policy authoring bug and raises immediately.
_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "in": lambda a, b: a in b,
}


@functools.lru_cache(maxsize=None)
def _load_policy(policy_id: str) -> dict[str, Any]:
    """Load and cache a policy document by its logical id.

    A policy_id like 'fee.late.courtesy_waiver' resolves to the newest version
    file matching 'fee.late.courtesy_waiver.v*.yaml' in the policy directory.
    """
    policy_dir = Path(config.POLICY_DIR)
    candidates = sorted(policy_dir.glob(f"{policy_id}.v*.yaml"))
    if not candidates:
        raise PolicyNotFound(f"No policy file for '{policy_id}' in {policy_dir}")
    # Highest version wins (lexical sort is fine for v1..v9; extend if needed).
    with open(candidates[-1], "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict) or "rules" not in doc:
        raise PolicyNotFound(f"Malformed policy document: {candidates[-1]}")
    return doc


def _condition_met(cond: dict[str, Any], facts: dict[str, Any], policy_id: str) -> bool:
    """Evaluate one `{field, op, value}` condition against account facts."""
    field = cond["field"]
    op = cond["op"]
    expected = cond["value"]

    if field not in facts:
        raise InsufficientData(
            f"Policy '{policy_id}' requires fact '{field}' which was not provided."
        )
    if op not in _OPS:
        raise PolicyNotFound(f"Policy '{policy_id}' uses unknown operator '{op}'.")

    return _OPS[op](facts[field], expected)


def evaluate(policy_id: str, account_facts: dict[str, Any]) -> Decision:
    """Return a deterministic, self-explaining Decision for this policy + facts.

    The first rule whose conditions ALL match wins. If no rule matches, the
    policy's `default` block is returned. `inputs_used` records exactly the
    facts the winning branch (or the whole rule scan) read — this is what makes
    the decision auditable.
    """
    policy = _load_policy(policy_id)
    version = policy.get("version", "unversioned")

    for rule in policy["rules"]:
        conditions = rule.get("when", [])
        # Record which facts this rule looked at, whether or not it fires.
        read_fields = {
            c["field"]: account_facts.get(c["field"]) for c in conditions
        }
        if all(_condition_met(c, account_facts, policy_id) for c in conditions):
            return Decision(
                outcome=Outcome(rule["outcome"]),
                reason_code=rule["reason_code"],
                reason_text=" ".join(rule["reason_text"].split()),
                policy_id=policy_id,
                policy_version=version,
                inputs_used=read_fields,
                rule_id=rule.get("id"),
            )

    default = policy["default"]
    return Decision(
        outcome=Outcome(default["outcome"]),
        reason_code=default["reason_code"],
        reason_text=" ".join(default["reason_text"].split()),
        policy_id=policy_id,
        policy_version=version,
        inputs_used=dict(account_facts),
        rule_id="default",
    )
