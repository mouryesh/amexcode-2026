"""
backend/ledger.py

Append-only audit trail. Each row hashed with previous row's hash, so
editing any row breaks every hash after it.

append() writes a row. verify() walks chain and finds tampering.
Only backend/actions.py should call append().
"""

import hashlib
import json
from datetime import datetime, timezone

from backend.database import get_connection

GENESIS = "0" * 64


def _canonical(obj):
    """Stable JSON. sort_keys so same data always hashes the same."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _hash(prev_hash, timestamp, actor, action, inputs, decision):
    blob = prev_hash + timestamp + actor + action + inputs + decision
    return hashlib.sha256(blob.encode()).hexdigest()


def append(actor, action, inputs, decision, session_id=None):
    """Add one row to the chain. Returns the new record_hash."""
    conn = get_connection()
    row = conn.execute(
        "SELECT record_hash FROM audit_ledger ORDER BY record_id DESC LIMIT 1"
    ).fetchone()
    prev_hash = row["record_hash"] if row else GENESIS

    timestamp = datetime.now(timezone.utc).isoformat()
    inputs_json = _canonical(inputs)
    decision_json = _canonical(decision)
    record_hash = _hash(prev_hash, timestamp, actor, action, inputs_json, decision_json)

    conn.execute(
        "INSERT INTO audit_ledger (session_id, timestamp, actor, action, inputs,"
        " decision, prev_hash, record_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, timestamp, actor, action, inputs_json, decision_json,
         prev_hash, record_hash),
    )
    conn.commit()
    conn.close()
    return record_hash


def verify():
    """Recompute every hash. Returns OK, or names the first bad record."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM audit_ledger ORDER BY record_id").fetchall()
    conn.close()

    prev_hash = GENESIS
    for r in rows:
        expected = _hash(prev_hash, r["timestamp"], r["actor"], r["action"],
                         r["inputs"], r["decision"])
        if r["prev_hash"] != prev_hash or r["record_hash"] != expected:
            return {"status": "TAMPERED", "broken_at_record_id": r["record_id"]}
        prev_hash = r["record_hash"]
    return {"status": "OK", "records": len(rows)}


def records_for_session(session_id):
    """All ledger rows for one session, in chain order."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM audit_ledger WHERE session_id = ? ORDER BY record_id",
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    # Self-check: clean chain verifies, tampered chain gets caught.
    import os

    import backend.database as db

    db.DB_PATH = "_ledger_selfcheck.db"
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init_db()

    for i in range(3):
        append("agent", "reverse_fee", {"amount": 500, "n": i},
               {"outcome": "APPROVE"}, session_id="s1")

    assert verify()["status"] == "OK", "clean chain must verify"

    conn = get_connection()
    conn.execute("UPDATE audit_ledger SET inputs = ? WHERE record_id = 2",
                 ('{"amount":99999}',))
    conn.commit()
    conn.close()

    result = verify()
    assert result["status"] == "TAMPERED", "tamper must be caught"
    assert result["broken_at_record_id"] == 2, result

    os.remove(db.DB_PATH)
    print("ledger self-check passed:", result)
