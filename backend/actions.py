"""
backend/actions.py

Write-side. The only file that changes account data and the only file that
calls ledger.append().

Every function follows the same 4 steps:
    1. Check idempotency key -> if seen before, return the old result.
    2. Change the table(s).
    3. Write a proof row to the audit ledger.
    4. Return a receipt (dict).
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from backend.database import get_connection
from backend.ledger import _canonical, append
from shared.exceptions import IdempotencyConflict


def _hash_inputs(inputs):
    return hashlib.sha256(_canonical(inputs).encode()).hexdigest()


def _check_idempotency(key, inputs):
    """Return the stored result if this key ran before, else None.
    Same key + different inputs is a bug, so raise."""
    conn = get_connection()
    row = conn.execute(
        "SELECT inputs_hash, result FROM idempotency_keys WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    if row["inputs_hash"] != _hash_inputs(inputs):
        raise IdempotencyConflict(f"key reused with different inputs: {key}")
    return json.loads(row["result"])


def _record_idempotency(key, action, inputs, result):
    conn = get_connection()
    conn.execute(
        "INSERT INTO idempotency_keys (idempotency_key, action, inputs_hash,"
        " result, created_at) VALUES (?, ?, ?, ?, ?)",
        (key, action, _hash_inputs(inputs), _canonical(result),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def _ref():
    return "REF-" + uuid.uuid4().hex[:8].upper()


def _receipt(action, amount=None, new_balance=None, detail=None, reference=None):
    """Shape matches shared.schemas.Receipt (dict until Pydantic lands)."""
    return {
        "reference": reference or _ref(),
        "action": action,
        "amount": amount,
        "currency": "INR",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "new_balance": new_balance,
        "detail": detail,
    }


def _balance(conn, member_id):
    row = conn.execute(
        "SELECT balance FROM accounts WHERE member_id = ?", (member_id,)
    ).fetchone()
    return row["balance"] if row else None


# --- The write actions ------------------------------------------------------

def reverse_fee(member_id, fee_amount, txn_ref, decision, idempotency_key):
    inputs = {"member_id": member_id, "fee_amount": fee_amount, "txn_ref": txn_ref}
    cached = _check_idempotency(idempotency_key, inputs)
    if cached:
        return cached

    conn = get_connection()
    conn.execute(
        "UPDATE transactions SET reversed = 1 WHERE txn_ref = ? AND member_id = ?",
        (txn_ref, member_id),
    )
    conn.execute(
        "UPDATE accounts SET balance = balance - ? WHERE member_id = ?",
        (fee_amount, member_id),
    )
    new_balance = _balance(conn, member_id)
    conn.commit()
    conn.close()

    append("agent", "reverse_fee", inputs, decision)
    receipt = _receipt("reverse_fee", amount=fee_amount, new_balance=new_balance,
                       detail=f"Late fee reversed on {txn_ref}")
    _record_idempotency(idempotency_key, "reverse_fee", inputs, receipt)
    return receipt


def block_card(member_id, card_id, reason, idempotency_key):
    inputs = {"member_id": member_id, "card_id": card_id, "reason": reason}
    cached = _check_idempotency(idempotency_key, inputs)
    if cached:
        return cached

    conn = get_connection()
    conn.execute(
        "UPDATE accounts SET card_status = 'blocked' WHERE member_id = ?",
        (member_id,),
    )
    conn.commit()
    conn.close()

    append("agent", "block_card", inputs, {"outcome": "APPROVE"})
    receipt = _receipt("block_card",
                       detail=f"Card {card_id} blocked: {reason}")
    _record_idempotency(idempotency_key, "block_card", inputs, receipt)
    return receipt


def issue_replacement(member_id, card_id, idempotency_key):
    inputs = {"member_id": member_id, "card_id": card_id}
    cached = _check_idempotency(idempotency_key, inputs)
    if cached:
        return cached

    conn = get_connection()
    conn.execute(
        "UPDATE accounts SET card_status = 'replacement_issued' WHERE member_id = ?",
        (member_id,),
    )
    conn.commit()
    conn.close()

    append("agent", "issue_replacement", inputs, {"outcome": "APPROVE"})
    new_card_ref = _ref()
    receipt = _receipt("issue_replacement", reference=new_card_ref,
                       detail=f"Replacement issued for {card_id}")
    _record_idempotency(idempotency_key, "issue_replacement", inputs, receipt)
    return receipt


def adjust_credit_limit(member_id, new_limit, decision, idempotency_key):
    inputs = {"member_id": member_id, "new_limit": new_limit}
    cached = _check_idempotency(idempotency_key, inputs)
    if cached:
        return cached

    conn = get_connection()
    conn.execute(
        "UPDATE accounts SET credit_limit = ? WHERE member_id = ?",
        (new_limit, member_id),
    )
    conn.commit()
    conn.close()

    append("agent", "adjust_credit_limit", inputs, decision)
    receipt = _receipt("adjust_credit_limit", amount=new_limit,
                       detail=f"Credit limit set to {new_limit}")
    _record_idempotency(idempotency_key, "adjust_credit_limit", inputs, receipt)
    return receipt


def open_underwriting_case(member_id, request_type, decision, idempotency_key):
    inputs = {"member_id": member_id, "request_type": request_type}
    cached = _check_idempotency(idempotency_key, inputs)
    if cached:
        return cached

    # No cases table (out of scope) — the ledger row IS the queued case.
    append("agent", "open_underwriting_case", inputs, decision)
    receipt = _receipt("open_underwriting_case",
                       detail=f"Underwriting case queued for {request_type}")
    _record_idempotency(idempotency_key, "open_underwriting_case", inputs, receipt)
    return receipt


def flag_vulnerability(member_id, signals, idempotency_key):
    inputs = {"member_id": member_id, "signals": signals}
    cached = _check_idempotency(idempotency_key, inputs)
    if cached:
        return cached

    conn = get_connection()
    conn.execute(
        "UPDATE accounts SET vulnerability = 1 WHERE member_id = ?", (member_id,)
    )
    conn.commit()
    conn.close()

    append("agent", "flag_vulnerability", inputs, {"outcome": "ESCALATE"})
    receipt = _receipt("flag_vulnerability",
                       detail="Vulnerability flagged; collections suppressed")
    _record_idempotency(idempotency_key, "flag_vulnerability", inputs, receipt)
    return receipt


if __name__ == "__main__":
    # Self-check: fee reverses once, idempotency blocks the double, ledger clean.
    import os

    import backend.database as db
    from backend.ledger import verify

    db.DB_PATH = "_actions_selfcheck.db"
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init_db()

    conn = get_connection()
    conn.execute("INSERT INTO accounts (member_id, name, tenure_months, balance)"
                 " VALUES ('M1', 'Priya', 36, 1000)")
    conn.execute("INSERT INTO transactions (txn_ref, member_id, posted_at, amount,"
                 " description) VALUES ('TXN-1', 'M1', '2026-01-01', 500, 'late fee')")
    conn.commit()
    conn.close()

    decision = {"outcome": "APPROVE", "policy": "fee.late.courtesy_waiver.v1"}
    r1 = reverse_fee("M1", 500, "TXN-1", decision, idempotency_key="k1")
    assert r1["new_balance"] == 500, r1

    # Same key again: must return same receipt, must NOT subtract twice.
    r2 = reverse_fee("M1", 500, "TXN-1", decision, idempotency_key="k1")
    assert r2 == r1, "idempotent replay must match"

    conn = get_connection()
    bal = _balance(conn, "M1")
    conn.close()
    assert bal == 500, f"balance double-charged: {bal}"

    assert verify()["status"] == "OK"

    os.remove(db.DB_PATH)
    print("actions self-check passed. balance:", bal, "| receipt:", r1["reference"])
