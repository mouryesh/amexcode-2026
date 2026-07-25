"""backend/actions.py — write-side of the fake core banking system.

Owned by Person B. Every function performs one ATOMIC, SERIALISED unit of work
inside a single write_transaction():
  1. Check the idempotency key — if already processed with the same inputs,
     return the original result. Same key + different inputs → IdempotencyConflict.
  2. Mutate the relevant table(s).
  3. Append the hash-chained ledger row (via ledger.append_on_conn, same txn).
  4. Record the idempotency key.
All four steps commit together or roll back together. Because the transaction
holds the write lock from the start (BEGIN IMMEDIATE), the ledger's
read-latest-hash-then-insert can never interleave with another action, so the
chain cannot fork under concurrency.

This is the SOLE ledger writer in the codebase. No other file appends.

Imports: backend.database, backend.ledger, shared.schemas, shared.exceptions.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from backend import ledger
from backend.database import write_transaction
from shared.exceptions import IdempotencyConflict
from shared.schemas import Receipt


def _inputs_hash(inputs: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _execute(
    idempotency_key: str,
    inputs: dict[str, Any],
    action_name: str,
    decision: Optional[dict[str, Any]],
    session_id: Optional[str],
    work: Callable[[Any], Receipt],
) -> Receipt:
    """Run one write action atomically and exactly once.

    `work(conn)` performs the table mutation on the shared transaction and
    returns the Receipt. The idempotency check, the ledger append, and the
    idempotency record all happen on the SAME connection/transaction, so a
    crash can never leave a mutation without its ledger row (or a completed
    action without its idempotency guard).
    """
    ih = _inputs_hash(inputs)
    with write_transaction() as conn:
        existing = conn.execute(
            "SELECT inputs_hash, result FROM idempotency_keys WHERE key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            if existing["inputs_hash"] != ih:
                raise IdempotencyConflict(
                    f"Idempotency key '{idempotency_key}' reused with different inputs."
                )
            return Receipt(**json.loads(existing["result"]))

        receipt = work(conn)
        ledger.append_on_conn(conn, "system", action_name, inputs, decision, session_id)
        conn.execute(
            "INSERT INTO idempotency_keys (key, inputs_hash, result, created_at) VALUES (?, ?, ?, ?)",
            (idempotency_key, ih, receipt.model_dump_json(),
             datetime.now(timezone.utc).isoformat()),
        )
        return receipt


# --------------------------------------------------------------------------- #
# Write actions
# --------------------------------------------------------------------------- #
def reverse_fee(
    member_id: str,
    fee_amount: float,
    txn_ref: str,
    decision: dict[str, Any],
    idempotency_key: str,
    session_id: Optional[str] = None,
) -> Receipt:
    inputs = {"member_id": member_id, "fee_amount": fee_amount, "txn_ref": txn_ref}

    def work(conn) -> Receipt:
        conn.execute(
            "UPDATE accounts SET balance = balance - ? WHERE member_id = ?",
            (fee_amount, member_id),
        )
        conn.execute(
            "UPDATE transactions SET reversed = 1 WHERE txn_ref = ?", (txn_ref,)
        )
        new_balance = conn.execute(
            "SELECT balance FROM accounts WHERE member_id = ?", (member_id,)
        ).fetchone()["balance"]
        return Receipt(
            reference=_ref("TXN"),
            action="reverse_fee",
            amount=fee_amount,
            new_balance=new_balance,
            detail=f"Late fee of {fee_amount} reversed against {txn_ref}; statement recalculated.",
        )

    return _execute(idempotency_key, inputs, "reverse_fee", decision, session_id, work)


def block_card(
    member_id: str,
    card_id: str,
    reason: str,
    idempotency_key: str,
    session_id: Optional[str] = None,
) -> Receipt:
    inputs = {"member_id": member_id, "card_id": card_id, "reason": reason}

    def work(conn) -> Receipt:
        conn.execute(
            "UPDATE accounts SET card_status = 'blocked' WHERE member_id = ?",
            (member_id,),
        )
        return Receipt(
            reference=_ref("BLK"),
            action="block_card",
            detail=f"Card {card_id} blocked ({reason}).",
        )

    return _execute(idempotency_key, inputs, "block_card", None, session_id, work)


def issue_replacement(
    member_id: str,
    card_id: str,
    idempotency_key: str,
    session_id: Optional[str] = None,
) -> Receipt:
    inputs = {"member_id": member_id, "card_id": card_id}

    def work(conn) -> Receipt:
        new_ref = _ref("CARD")
        conn.execute(
            """INSERT INTO transactions (txn_ref, member_id, date, amount, description, kind)
               VALUES (?, ?, ?, 0, ?, 'replacement')""",
            (
                new_ref,
                member_id,
                datetime.now(timezone.utc).isoformat(),
                f"Replacement card issued for {card_id}",
            ),
        )
        return Receipt(
            reference=new_ref,
            action="issue_replacement",
            detail=f"Replacement card issued for {card_id}. Arrives in 5–7 business days.",
        )

    return _execute(idempotency_key, inputs, "issue_replacement", None, session_id, work)


def adjust_credit_limit(
    member_id: str,
    new_limit: float,
    decision: dict[str, Any],
    idempotency_key: str,
    session_id: Optional[str] = None,
) -> Receipt:
    inputs = {"member_id": member_id, "new_limit": new_limit}

    def work(conn) -> Receipt:
        conn.execute(
            "UPDATE accounts SET credit_limit = ? WHERE member_id = ?",
            (new_limit, member_id),
        )
        return Receipt(
            reference=_ref("LMT"),
            action="adjust_credit_limit",
            amount=new_limit,
            detail=f"Credit limit updated to {new_limit}.",
        )

    return _execute(idempotency_key, inputs, "adjust_credit_limit", decision, session_id, work)


def open_underwriting_case(
    member_id: str,
    request_type: str,
    decision: dict[str, Any],
    idempotency_key: str,
    session_id: Optional[str] = None,
) -> Receipt:
    inputs = {"member_id": member_id, "request_type": request_type}
    case_ref = _ref("CASE")

    def work(conn) -> Receipt:
        # No cases table (out of scope) — the ledger row IS the queued case.
        return Receipt(
            reference=case_ref,
            action="open_underwriting_case",
            detail=f"Underwriting case {case_ref} opened for {request_type}.",
        )

    return _execute(idempotency_key, inputs, "open_underwriting_case", decision, session_id, work)


def flag_vulnerability(
    member_id: str,
    signals: list[str],
    idempotency_key: str,
    session_id: Optional[str] = None,
) -> Receipt:
    inputs = {"member_id": member_id, "signals": signals}

    def work(conn) -> Receipt:
        conn.execute(
            "UPDATE accounts SET vulnerability_flag = 1 WHERE member_id = ?",
            (member_id,),
        )
        return Receipt(
            reference=_ref("VUL"),
            action="flag_vulnerability",
            detail="Account flagged for sensitive handling; collections activity suppressed.",
        )

    return _execute(idempotency_key, inputs, "flag_vulnerability", None, session_id, work)
