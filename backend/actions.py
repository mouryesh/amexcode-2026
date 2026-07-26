"""backend/actions.py — write-side of the fake core banking system.

Every function is one ATOMIC, SERIALISED unit of work inside a single
write_transaction():

  1. Check the idempotency key — same key + same inputs returns the original
     result; same key + different inputs is a conflict.
  2. Mutate.
  3. Append the hash-chained ledger row on the same transaction.
  4. Read the effect back.
  5. Record the idempotency key.

All of it commits together or rolls back together, and the transaction holds the
chain advisory lock from the top, so the ledger's read-latest-hash-then-insert
can never interleave and the chain cannot fork.

Effect verification is a read-back on the same transaction, which against a
local Postgres is close to tautological — it earns its keep when this points at
a real core, where a timeout means indeterminate rather than failed. Keeping the
branch here now means the caller already handles the third outcome.

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
from shared.exceptions import EffectUnverified, IdempotencyConflict
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
    verify: Optional[Callable[[Any], bool]] = None,
) -> Receipt:
    """Run one write action atomically and exactly once."""
    ih = _inputs_hash(inputs)
    with write_transaction() as conn:
        existing = conn.execute(
            "SELECT inputs_hash, result FROM idempotency_keys WHERE key = %s",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            if existing["inputs_hash"] != ih:
                raise IdempotencyConflict(
                    f"Idempotency key '{idempotency_key}' reused with different inputs."
                )
            return Receipt(**json.loads(existing["result"]))

        receipt = work(conn)

        if verify is not None and not verify(conn):
            # Rolls back the mutation and the ledger row together. The caller
            # surfaces T-INDETERMINATE and escalates; it must not retry.
            raise EffectUnverified(
                f"{action_name} submitted but the effect could not be verified."
            )

        ledger.emit_tool(
            conn,
            session_id=session_id or "",
            member_ref=inputs.get("member_id"),
            tool=action_name,
            params=inputs,
            decision=decision,
        )
        conn.execute(
            """INSERT INTO idempotency_keys (key, action, inputs_hash, result, created_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (idempotency_key, action_name, ih, receipt.model_dump_json(),
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
            "UPDATE accounts SET balance = balance - %s WHERE member_id = %s",
            (fee_amount, member_id),
        )
        conn.execute(
            "UPDATE transactions SET reversed = TRUE WHERE txn_ref = %s", (txn_ref,)
        )
        new_balance = float(
            conn.execute(
                "SELECT balance FROM accounts WHERE member_id = %s", (member_id,)
            ).fetchone()["balance"]
        )
        return Receipt(
            reference=_ref("TXN"),
            action="reverse_fee",
            amount=fee_amount,
            new_balance=new_balance,
            detail=f"Late fee of {fee_amount} reversed against {txn_ref}; statement recalculated.",
        )

    def verify(conn) -> bool:
        row = conn.execute(
            "SELECT reversed FROM transactions WHERE txn_ref = %s", (txn_ref,)
        ).fetchone()
        return bool(row and row["reversed"])

    return _execute(idempotency_key, inputs, "reverse_fee", decision, session_id, work, verify)


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
            "UPDATE accounts SET card_status = 'blocked' WHERE member_id = %s", (member_id,)
        )
        return Receipt(
            reference=_ref("BLK"), action="block_card",
            detail=f"Card {card_id} blocked ({reason}).",
        )

    def verify(conn) -> bool:
        row = conn.execute(
            "SELECT card_status FROM accounts WHERE member_id = %s", (member_id,)
        ).fetchone()
        return bool(row and row["card_status"] == "blocked")

    return _execute(idempotency_key, inputs, "block_card", None, session_id, work, verify)


def issue_replacement(
    member_id: str,
    card_id: str,
    idempotency_key: str,
    session_id: Optional[str] = None,
) -> Receipt:
    inputs = {"member_id": member_id, "card_id": card_id}
    new_ref = _ref("CARD")

    def work(conn) -> Receipt:
        conn.execute(
            """INSERT INTO transactions (txn_ref, member_id, date, amount, description, kind)
               VALUES (%s, %s, %s, 0, %s, 'replacement')""",
            (new_ref, member_id, datetime.now(timezone.utc).isoformat(),
             f"Replacement card issued for {card_id}"),
        )
        return Receipt(
            reference=new_ref, action="issue_replacement",
            detail=f"Replacement card issued for {card_id}. Arrives in 5-7 business days.",
        )

    def verify(conn) -> bool:
        return conn.execute(
            "SELECT 1 FROM transactions WHERE txn_ref = %s", (new_ref,)
        ).fetchone() is not None

    return _execute(idempotency_key, inputs, "issue_replacement", None, session_id, work, verify)


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
            "UPDATE accounts SET credit_limit = %s WHERE member_id = %s",
            (new_limit, member_id),
        )
        return Receipt(
            reference=_ref("LMT"), action="adjust_credit_limit", amount=new_limit,
            detail=f"Credit limit updated to {new_limit}.",
        )

    def verify(conn) -> bool:
        row = conn.execute(
            "SELECT credit_limit FROM accounts WHERE member_id = %s", (member_id,)
        ).fetchone()
        return bool(row and float(row["credit_limit"]) == float(new_limit))

    return _execute(
        idempotency_key, inputs, "adjust_credit_limit", decision, session_id, work, verify
    )


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
            reference=case_ref, action="open_underwriting_case",
            detail=f"Underwriting case {case_ref} opened for {request_type}.",
        )

    return _execute(
        idempotency_key, inputs, "open_underwriting_case", decision, session_id, work
    )


def flag_vulnerability(
    member_id: str,
    signals: list[str],
    idempotency_key: str,
    session_id: Optional[str] = None,
) -> Receipt:
    inputs = {"member_id": member_id, "signals": signals}

    def work(conn) -> Receipt:
        conn.execute(
            "UPDATE accounts SET vulnerability_flag = TRUE WHERE member_id = %s", (member_id,)
        )
        return Receipt(
            reference=_ref("VUL"), action="flag_vulnerability",
            detail="Account flagged for sensitive handling; collections activity suppressed.",
        )

    def verify(conn) -> bool:
        row = conn.execute(
            "SELECT vulnerability_flag FROM accounts WHERE member_id = %s", (member_id,)
        ).fetchone()
        return bool(row and row["vulnerability_flag"])

    return _execute(
        idempotency_key, inputs, "flag_vulnerability", None, session_id, work, verify
    )
