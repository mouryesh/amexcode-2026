"""backend/ledger.py — the entire audit trail logic. Two public functions.

Owned by Person B. append() is the sole writer of ledger rows (only
backend/actions.py calls it). verify() walks the chain and proves integrity.

Imports: shared.schemas, backend.database.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from backend.database import db_session, write_transaction

_GENESIS = "0" * 64


def _canonical(obj: Any) -> str:
    """Deterministic JSON: sort_keys=True so hashes reproduce across runs.

    Field order matters or the chain won't re-verify.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _compute_hash(
    prev_hash: str,
    timestamp: str,
    actor: str,
    action: str,
    inputs: dict[str, Any],
    decision: Optional[dict[str, Any]],
) -> str:
    payload = (
        prev_hash
        + timestamp
        + actor
        + action
        + _canonical(inputs)
        + _canonical(decision)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _latest_hash(conn) -> str:
    row = conn.execute(
        "SELECT record_hash FROM audit_ledger ORDER BY record_id DESC LIMIT 1"
    ).fetchone()
    return row["record_hash"] if row else _GENESIS


def append_on_conn(
    conn,
    actor: str,
    action: str,
    inputs: dict[str, Any],
    decision: Optional[dict[str, Any]] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Append one hash-chained record ON AN EXISTING (write-locked) transaction.

    This is the atomic building block: backend/actions.py calls it inside its
    own write_transaction() so the account mutation and this ledger row commit
    together. The caller MUST hold the write lock (BEGIN IMMEDIATE) so the
    read-latest-hash-then-insert below cannot interleave with another append.
    Does NOT commit — the caller's transaction owns that.
    """
    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).isoformat()
    prev_hash = _latest_hash(conn)
    record_hash = _compute_hash(prev_hash, timestamp, actor, action, inputs, decision)
    cur = conn.execute(
        """INSERT INTO audit_ledger
           (session_id, timestamp, actor, action, inputs, decision, prev_hash, record_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            timestamp,
            actor,
            action,
            _canonical(inputs),
            _canonical(decision) if decision is not None else None,
            prev_hash,
            record_hash,
        ),
    )
    return {
        "record_id": cur.lastrowid,
        "timestamp": timestamp,
        "record_hash": record_hash,
        "prev_hash": prev_hash,
    }


def append(
    actor: str,
    action: str,
    inputs: dict[str, Any],
    decision: Optional[dict[str, Any]] = None,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Append one hash-chained record in its own serialised transaction.

    Standalone entry point (tests, ad-hoc use). For the atomic action path the
    write goes through append_on_conn() on the caller's transaction instead.

    record_hash = sha256(prev_hash + timestamp + actor + action + inputs + decision)
    """
    with write_transaction() as conn:
        return append_on_conn(conn, actor, action, inputs, decision, session_id)


def verify() -> dict[str, Any]:
    """Walk every record in chain order and recompute each hash.

    Returns {"status": "OK", "records": n} when the chain is intact, or
    {"status": "TAMPERED", "broken_at_record_id": id} at the first break.
    """
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_ledger ORDER BY record_id ASC"
        ).fetchall()

        prev_hash = _GENESIS
        for row in rows:
            inputs = json.loads(row["inputs"])
            decision = json.loads(row["decision"]) if row["decision"] else None

            # 1. The stored prev_hash must match the actual predecessor hash.
            if row["prev_hash"] != prev_hash:
                return {"status": "TAMPERED", "broken_at_record_id": row["record_id"]}

            # 2. Recompute this row's hash from its own contents.
            expected = _compute_hash(
                row["prev_hash"],
                row["timestamp"],
                row["actor"],
                row["action"],
                inputs,
                decision,
            )
            if expected != row["record_hash"]:
                return {"status": "TAMPERED", "broken_at_record_id": row["record_id"]}

            prev_hash = row["record_hash"]

        return {"status": "OK", "records": len(rows)}


def records_for_session(session_id: str) -> list[dict[str, Any]]:
    """Return every ledger record for a session, in chain order."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_ledger WHERE session_id = ? ORDER BY record_id ASC",
            (session_id,),
        ).fetchall()
        return [
            {
                "record_id": r["record_id"],
                "session_id": r["session_id"],
                "timestamp": r["timestamp"],
                "actor": r["actor"],
                "action": r["action"],
                "inputs": json.loads(r["inputs"]),
                "decision": json.loads(r["decision"]) if r["decision"] else None,
                "prev_hash": r["prev_hash"],
                "record_hash": r["record_hash"],
            }
            for r in rows
        ]
