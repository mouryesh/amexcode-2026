"""backend/ledger.py — the audit trail. Append-only, hash-chained, on PostgreSQL.

What changed from the SQLite version, and why it matters: the old ledger was
written **only** by `backend/actions.py`, so only successful writes produced a
row. A decline — the case a member is most likely to challenge — left no trace
at all. This module now exposes an emitter per event class, and every stage of
the graph calls one:

    emit_turn      one member message or agent reply, redacted
    emit_policy    a policy evaluation and the decision it produced
    emit_tool      a write action: parameters, idempotency key, read-back
    emit_security  a secret redaction, injection refusal or loop break
    emit_terminal  session close: outcome, receipt, chain head

Chain integrity:

    record_hash = sha256(prev_hash + occurred_at + event_type + actor + action
                         + canonical(inputs) + canonical(decision))

`canonical()` is sorted-key, whitespace-free JSON. Field order is part of the
hash, so it has to reproduce byte-for-byte on read-back — which is why the
ledger stores these as TEXT rather than JSONB (see the DDL comment in
`database.py`).

Callers inside a write transaction pass `conn` so the audit row commits in the
same transaction as the mutation it describes. There is no code path that
commits a mutation without its ledger row.

Imports: backend.database, shared.config.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from backend.database import db_session, queue_audit_event, write_transaction

_GENESIS = "0" * 64

# The five event classes. Anything not in this set is a programming error.
EVENT_TYPES = frozenset({"turn", "policy", "tool", "security", "terminal"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(obj: Any) -> str:
    """Deterministic JSON. Sorted keys and no whitespace, or hashes won't reproduce."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _compute_hash(
    prev_hash: str,
    occurred_at: str,
    event_type: str,
    actor: str,
    action: str,
    inputs: dict[str, Any],
    decision: Optional[dict[str, Any]],
) -> str:
    payload = (
        prev_hash
        + occurred_at
        + event_type
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


# --------------------------------------------------------------------------- #
# Core append
# --------------------------------------------------------------------------- #
def append_on_conn(
    conn,
    event_type: str,
    actor: str,
    action: str,
    inputs: dict[str, Any],
    decision: Optional[dict[str, Any]] = None,
    session_id: Optional[str] = None,
    member_ref: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Append one chained record on an existing, lock-holding transaction.

    The caller must hold the chain advisory lock (i.e. be inside
    `write_transaction()`), or the read-latest-hash-then-insert below can
    interleave with another writer and fork the chain. Does not commit — the
    caller's transaction owns that.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown audit event_type '{event_type}'.")

    occurred_at = _now_iso()
    prev_hash = _latest_hash(conn)
    record_hash = _compute_hash(
        prev_hash, occurred_at, event_type, actor, action, inputs, decision
    )
    inputs_json = _canonical(inputs)
    decision_json = _canonical(decision) if decision is not None else None

    row = conn.execute(
        """INSERT INTO audit_ledger
              (trace_id, session_id, member_ref, event_type, actor, action,
               occurred_at, inputs, decision, prev_hash, record_hash)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING record_id""",
        (
            trace_id, session_id, member_ref, event_type, actor, action,
            occurred_at, inputs_json, decision_json, prev_hash, record_hash,
        ),
    ).fetchone()

    record = {
        "record_id": row["record_id"],
        "trace_id": trace_id,
        "session_id": session_id,
        "member_ref": member_ref,
        "event_type": event_type,
        "actor": actor,
        "action": action,
        "occurred_at": occurred_at,
        "inputs": inputs,
        "decision": decision,
        "prev_hash": prev_hash,
        "record_hash": record_hash,
    }
    # Held until this transaction commits, then mirrored to Splunk.
    queue_audit_event(record)
    return record


def append(
    event_type: str,
    actor: str,
    action: str,
    inputs: dict[str, Any],
    decision: Optional[dict[str, Any]] = None,
    session_id: Optional[str] = None,
    member_ref: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Append one record in its own serialised transaction.

    Use this for events with no accompanying mutation — turns, policy
    evaluations, security events. Write actions go through `actions.py`, which
    calls `append_on_conn` on the transaction carrying the mutation.
    """
    with write_transaction() as conn:
        return append_on_conn(
            conn, event_type, actor, action, inputs, decision,
            session_id, member_ref, trace_id,
        )


# --------------------------------------------------------------------------- #
# Emitters — one per event class in the audit contract
# --------------------------------------------------------------------------- #
def emit_turn(
    session_id: str,
    member_ref: Optional[str],
    role: str,
    action: str,
    payload: dict[str, Any],
    trace_id: Optional[str] = None,
    conn=None,
) -> dict[str, Any]:
    """A member message or an agent reply.

    `payload` carries the redacted text and its hash, the dialogue act, intent
    candidates with confidence and margin, risk flags, and the template id of
    whatever was rendered. Never the raw message if it contained a secret.
    """
    fn = append_on_conn if conn is not None else append
    args = (conn,) if conn is not None else ()
    return fn(*args, "turn", role, action, payload, None, session_id, member_ref, trace_id)


def emit_policy(
    session_id: str,
    member_ref: Optional[str],
    decision: dict[str, Any],
    inputs_used: dict[str, Any],
    trace_id: Optional[str] = None,
    conn=None,
) -> dict[str, Any]:
    """A policy evaluation.

    This is the row that makes a decline auditable. `inputs_used` must be the
    exact fact dict the policy read, so an auditor can re-run `evaluate` against
    it and reproduce the outcome.
    """
    fn = append_on_conn if conn is not None else append
    args = (conn,) if conn is not None else ()
    action = f"evaluate:{decision.get('policy_id', 'unknown')}"
    return fn(*args, "policy", "agent", action, inputs_used, decision,
              session_id, member_ref, trace_id)


def emit_tool(
    conn,
    session_id: str,
    member_ref: Optional[str],
    tool: str,
    params: dict[str, Any],
    decision: Optional[dict[str, Any]] = None,
    trace_id: Optional[str] = None,
) -> dict[str, Any]:
    """A write action. Always on the mutating transaction — `conn` is required."""
    return append_on_conn(
        conn, "tool", "system", tool, params, decision, session_id, member_ref, trace_id
    )


def emit_security(
    session_id: Optional[str],
    member_ref: Optional[str],
    action: str,
    detail: dict[str, Any],
    trace_id: Optional[str] = None,
    conn=None,
) -> dict[str, Any]:
    """A redaction, injection refusal, abuse boundary or loop break.

    `detail` records the detection class and that redaction was applied. It must
    never contain the secret value — that is the entire point of the node that
    triggered this.
    """
    fn = append_on_conn if conn is not None else append
    args = (conn,) if conn is not None else ()
    return fn(*args, "security", "system", action, detail, None,
              session_id, member_ref, trace_id)


def emit_terminal(
    session_id: str,
    member_ref: Optional[str],
    outcome: str,
    detail: dict[str, Any],
    trace_id: Optional[str] = None,
    conn=None,
) -> dict[str, Any]:
    """Session close: outcome, receipt or case reference, unresolved items."""
    fn = append_on_conn if conn is not None else append
    args = (conn,) if conn is not None else ()
    payload = {"outcome": outcome, **detail}
    return fn(*args, "terminal", "agent", f"close:{outcome}", payload, None,
              session_id, member_ref, trace_id)


# --------------------------------------------------------------------------- #
# Verification and reads
# --------------------------------------------------------------------------- #
def verify() -> dict[str, Any]:
    """Walk the chain in order and recompute every hash.

    Returns {"status": "OK", "records": n, "chain_head": hash} when intact, or
    {"status": "TAMPERED", "broken_at_record_id": id, "reason": ...} at the
    first break.
    """
    with db_session() as conn:
        rows = conn.execute(
            """SELECT record_id, event_type, actor, action, occurred_at,
                      inputs, decision, prev_hash, record_hash
               FROM audit_ledger ORDER BY record_id ASC"""
        ).fetchall()

        prev_hash = _GENESIS
        for r in rows:
            if r["prev_hash"] != prev_hash:
                return {
                    "status": "TAMPERED",
                    "broken_at_record_id": r["record_id"],
                    "reason": "prev_hash does not match the preceding record",
                }

            inputs = json.loads(r["inputs"])
            decision = json.loads(r["decision"]) if r["decision"] else None
            expected = _compute_hash(
                r["prev_hash"], r["occurred_at"], r["event_type"],
                r["actor"], r["action"], inputs, decision,
            )
            if expected != r["record_hash"]:
                return {
                    "status": "TAMPERED",
                    "broken_at_record_id": r["record_id"],
                    "reason": "record contents do not match the stored hash",
                }
            prev_hash = r["record_hash"]

        return {"status": "OK", "records": len(rows), "chain_head": prev_hash}


def records_for_session(session_id: str) -> list[dict[str, Any]]:
    """Every ledger record for a session, in chain order. Powers the audit viewer."""
    with db_session() as conn:
        rows = conn.execute(
            """SELECT record_id, trace_id, session_id, member_ref, event_type,
                      actor, action, occurred_at, inputs, decision,
                      prev_hash, record_hash
               FROM audit_ledger WHERE session_id = %s ORDER BY record_id ASC""",
            (session_id,),
        ).fetchall()
        return [
            {
                **{k: r[k] for k in (
                    "record_id", "trace_id", "session_id", "member_ref",
                    "event_type", "actor", "action", "occurred_at",
                    "prev_hash", "record_hash",
                )},
                "inputs": json.loads(r["inputs"]),
                "decision": json.loads(r["decision"]) if r["decision"] else None,
            }
            for r in rows
        ]


def anchor_head(session_id: Optional[str] = None) -> dict[str, Any]:
    """Record the current chain head so later tampering is detectable externally.

    Locally this writes a `chain_anchors` row. In production the same value goes
    to S3 Object Lock — the chain is only tamper-evident against a database
    administrator if the head is held somewhere the database cannot reach.
    """
    with write_transaction() as conn:
        row = conn.execute(
            "SELECT record_id, record_hash FROM audit_ledger ORDER BY record_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"anchored": False, "reason": "empty ledger"}
        anchored_at = _now_iso()
        conn.execute(
            """INSERT INTO chain_anchors (record_id, chain_head, session_id, anchored_at)
               VALUES (%s, %s, %s, %s)""",
            (row["record_id"], row["record_hash"], session_id, anchored_at),
        )
        return {
            "anchored": True,
            "record_id": row["record_id"],
            "chain_head": row["record_hash"],
            "anchored_at": anchored_at,
        }
