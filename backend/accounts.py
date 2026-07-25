"""backend/accounts.py — read-side of the fake core banking system.

Owned by Person B. Split from writes so the policy engine's inputs come from a
read-only surface: the policy engine never touches a function that could mutate
state.

Imports: backend.database, shared.schemas, shared.exceptions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from backend.database import db_session
from shared.exceptions import MemberNotFound


def _require_member(conn, member_id: str):
    row = conn.execute(
        "SELECT * FROM accounts WHERE member_id = ?", (member_id,)
    ).fetchone()
    if row is None:
        raise MemberNotFound(f"No account for member '{member_id}'.")
    return row


def get_member(member_id: str) -> dict[str, Any]:
    """Full account profile."""
    with db_session() as conn:
        row = _require_member(conn, member_id)
        return dict(row)


def get_balance(member_id: str) -> dict[str, Any]:
    """Current balance and credit limit."""
    with db_session() as conn:
        row = _require_member(conn, member_id)
        return {
            "balance": row["balance"],
            "credit_limit": row["credit_limit"],
            "utilisation": row["utilisation"],
        }


def get_waiver_history(member_id: str, months: int = 8) -> list[dict[str, Any]]:
    """Courtesy waivers within a trailing time window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30 * months)).isoformat()
    with db_session() as conn:
        _require_member(conn, member_id)
        rows = conn.execute(
            "SELECT * FROM waiver_history WHERE member_id = ? AND date >= ? ORDER BY date DESC",
            (member_id, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]


def get_transactions(member_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Recent charge history."""
    with db_session() as conn:
        _require_member(conn, member_id)
        rows = conn.execute(
            "SELECT * FROM transactions WHERE member_id = ? ORDER BY date DESC LIMIT ?",
            (member_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_income_data(member_id: str) -> dict[str, Any]:
    """Income staleness signal used by the credit policy."""
    with db_session() as conn:
        row = _require_member(conn, member_id)
        return {"income_staleness_months": row["income_staleness_months"]}


def get_account_facts(member_id: str, distress_signals: bool = False) -> dict[str, Any]:
    """Flatten the account into the fact dict the policy engine consumes.

    This is the single read-only surface `nodes.run_policy` uses to build the
    input to `policy_engine.evaluate`. Everything the policies reference by
    `field` name must be present here.
    """
    with db_session() as conn:
        row = _require_member(conn, member_id)
        waivers_8m = conn.execute(
            """SELECT COUNT(*) AS n FROM waiver_history
               WHERE member_id = ? AND date >= ?""",
            (
                member_id,
                (datetime.now(timezone.utc) - timedelta(days=30 * 8)).isoformat(),
            ),
        ).fetchone()["n"]
        replacements_30d = conn.execute(
            """SELECT COUNT(*) AS n FROM transactions
               WHERE member_id = ? AND kind = 'replacement' AND date >= ?""",
            (
                member_id,
                (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
            ),
        ).fetchone()["n"]

        return {
            "member_id": row["member_id"],
            "tenure_months": row["tenure_months"],
            "late_payments_12m": row["late_payments_12m"],
            "prior_waivers_8m": waivers_8m,
            "income_staleness_months": row["income_staleness_months"],
            "utilisation": row["utilisation"],
            "fraud_hold": bool(row["fraud_hold"]),
            "replacements_30d": replacements_30d,
            "distress_signals": distress_signals,
        }
