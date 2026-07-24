"""backend/actions.py — write-side of the fake core banking system.

Owned by Person B. Every function follows the same pattern:
  1. Check idempotency key — if already processed with the same inputs, return
     the original result. Same key + different inputs → IdempotencyConflict.
  2. Mutate the relevant table.
  3. Call ledger.append() to record the action with its decision.
  4. Return a structured Receipt.

This is the SOLE ledger writer in the codebase. No other file calls
ledger.append().

Imports: backend.database, backend.ledger, shared.schemas, shared.exceptions.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from backend import ledger
from backend.database import db_session
from shared.exceptions import IdempotencyConflict
from shared.schemas import Receipt


def _inputs_hash(inputs: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _idempotent(
    key: str,
    inputs: dict[str, Any],
    do_work: Callable[[], Receipt],
) -> Receipt:
    """Run `do_work` at most once per idempotency key.

    Replaying the same key with the same inputs returns the stored receipt.
    Replaying with different inputs is a caller bug → IdempotencyConflict.
    """
    ih = _inputs_hash(inputs)
    with db_session() as conn:
        existing = conn.execute(
            "SELECT inputs_hash, result FROM idempotency_keys WHERE key = ?", (key,)
        ).fetchone()
        if existing is not None:
            if existing["inputs_hash"] != ih:
                raise IdempotencyConflict(
                    f"Idempotency key '{key}' reused with different inputs."
                )
            return Receipt(**json.loads(existing["result"]))

    receipt = do_work()

    with db_session() as conn:
        conn.execute(
            "INSERT INTO idempotency_keys (key, inputs_hash, result, created_at) VALUES (?, ?, ?, ?)",
            (key, ih, receipt.model_dump_json(), datetime.now(timezone.utc).isoformat()),
        )
    return receipt


def _ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


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

    def work() -> Receipt:
        with db_session() as conn:
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
        ledger.append("system", "reverse_fee", inputs, decision, session_id)
        return Receipt(
            reference=_ref("TXN"),
            action="reverse_fee",
            amount=fee_amount,
            new_balance=new_balance,
            detail=f"Late fee of {fee_amount} reversed against {txn_ref}; statement recalculated.",
        )

    return _idempotent(idempotency_key, inputs, work)


def block_card(
    member_id: str,
    card_id: str,
    reason: str,
    idempotency_key: str,
    session_id: Optional[str] = None,
) -> Receipt:
    inputs = {"member_id": member_id, "card_id": card_id, "reason": reason}

    def work() -> Receipt:
        with db_session() as conn:
            conn.execute(
                "UPDATE accounts SET card_status = 'blocked' WHERE member_id = ?",
                (member_id,),
            )
        ledger.append("system", "block_card", inputs, None, session_id)
        return Receipt(
            reference=_ref("BLK"),
            action="block_card",
            detail=f"Card {card_id} blocked ({reason}).",
        )

    return _idempotent(idempotency_key, inputs, work)


def issue_replacement(
    member_id: str,
    card_id: str,
    idempotency_key: str,
    session_id: Optional[str] = None,
) -> Receipt:
    inputs = {"member_id": member_id, "card_id": card_id}

    def work() -> Receipt:
        new_ref = _ref("CARD")
        with db_session() as conn:
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
        ledger.append("system", "issue_replacement", inputs, None, session_id)
        return Receipt(
            reference=new_ref,
            action="issue_replacement",
            detail=f"Replacement card issued for {card_id}. Arrives in 5–7 business days.",
        )

    return _idempotent(idempotency_key, inputs, work)


def adjust_credit_limit(
    member_id: str,
    new_limit: float,
    decision: dict[str, Any],
    idempotency_key: str,
    session_id: Optional[str] = None,
) -> Receipt:
    inputs = {"member_id": member_id, "new_limit": new_limit}

    def work() -> Receipt:
        with db_session() as conn:
            conn.execute(
                "UPDATE accounts SET credit_limit = ? WHERE member_id = ?",
                (new_limit, member_id),
            )
        ledger.append("system", "adjust_credit_limit", inputs, decision, session_id)
        return Receipt(
            reference=_ref("LMT"),
            action="adjust_credit_limit",
            amount=new_limit,
            detail=f"Credit limit updated to {new_limit}.",
        )

    return _idempotent(idempotency_key, inputs, work)


def open_underwriting_case(
    member_id: str,
    request_type: str,
    decision: dict[str, Any],
    idempotency_key: str,
    session_id: Optional[str] = None,
) -> Receipt:
    inputs = {"member_id": member_id, "request_type": request_type}

    def work() -> Receipt:
        case_ref = _ref("CASE")
        ledger.append("system", "open_underwriting_case", inputs, decision, session_id)
        return Receipt(
            reference=case_ref,
            action="open_underwriting_case",
            detail=f"Underwriting case {case_ref} opened for {request_type}.",
        )

    return _idempotent(idempotency_key, inputs, work)


def flag_vulnerability(
    member_id: str,
    signals: list[str],
    idempotency_key: str,
    session_id: Optional[str] = None,
) -> Receipt:
    inputs = {"member_id": member_id, "signals": signals}

    def work() -> Receipt:
        with db_session() as conn:
            conn.execute(
                "UPDATE accounts SET vulnerability_flag = 1 WHERE member_id = ?",
                (member_id,),
            )
        ledger.append("system", "flag_vulnerability", inputs, None, session_id)
        return Receipt(
            reference=_ref("VUL"),
            action="flag_vulnerability",
            detail="Account flagged for sensitive handling; collections activity suppressed.",
        )

    return _idempotent(idempotency_key, inputs, work)
