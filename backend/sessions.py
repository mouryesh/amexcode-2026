"""backend/sessions.py — conversation history, per session.

Owned by Person B. The turn-by-turn record of a conversation: what the member
said, what the agent replied, and (for agent turns) which policy decision, if
any, produced that reply. This is what lets a stateless graph invocation see
real conversation history instead of just the current message, and lets the
agent layer detect repeated-request patterns (e.g. the same decline asked for
again) without the policy engine itself needing to know about sessions.

Writes to the reconciled `messages` table (see BACKEND_RECONCILIATION.md), the
single conversation transcript that both the agent layer and the audit-viewer
replay read from.

Text is redacted (shared.redaction) before it's persisted — PII AT REST, not
just in transit. This is the single choke point every persisted message goes
through, so redaction happens here once rather than at every call site.

Imports: backend.database, shared.redaction.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from backend.database import db_session
from shared.redaction import redact


def record_turn(
    session_id: str,
    member_id: Optional[str],
    role: str,
    text: str,
    intent: Optional[str] = None,
    confidence: Optional[float] = None,
    decision_outcome: Optional[str] = None,
    decision_reason_code: Optional[str] = None,
    policy_id: Optional[str] = None,
) -> None:
    """Append one turn (a member message or an agent reply) to the session.

    `turn_index` is assigned per session (0, 1, 2, ...) so the transcript has a
    stable, gap-free order independent of the global autoincrement id. `text`
    is redacted before storage — the member's live turn still gets processed
    against the raw, unredacted message (this function is only ever called
    AFTER the graph has already used it); only the persisted copy is stripped.
    """
    with db_session() as conn:
        next_turn = conn.execute(
            "SELECT COALESCE(MAX(turn_index), -1) + 1 AS n FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]
        conn.execute(
            """INSERT INTO messages
               (session_id, member_id, turn_index, role, content, intent, confidence,
                decision_outcome, decision_reason_code, policy_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id, member_id, next_turn, role, redact(text), intent, confidence,
                decision_outcome, decision_reason_code, policy_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_history(session_id: str) -> list[dict[str, str]]:
    """Prior turns for this session, in order, as the {role, text} shape AgentState expects."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? ORDER BY turn_index ASC",
            (session_id,),
        ).fetchall()
        return [{"role": r["role"], "text": r["content"]} for r in rows]


def count_prior_declines(session_id: str, policy_id: str, reason_code: str) -> int:
    """How many times this session has already been declined for this exact reason.

    Scoped to (policy_id, reason_code) so a decline on one policy never counts
    toward the repeat threshold of an unrelated request.
    """
    with db_session() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n FROM messages
               WHERE session_id = ? AND policy_id = ?
                 AND decision_outcome = 'DECLINE' AND decision_reason_code = ?""",
            (session_id, policy_id, reason_code),
        ).fetchone()
        return row["n"]
