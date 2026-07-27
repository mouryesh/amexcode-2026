"""backend/accounts.py — read-side of the fake core banking system.

Split from writes so the policy engine's inputs come from a read-only surface:
it never touches a function that could mutate state.

Postgres NUMERIC comes back as Decimal, which raises TypeError the moment it
meets a float in arithmetic. Every money field is cast to float on the way out
so the policy engine and the YAML thresholds stay in one numeric world.

Imports: backend.database, shared.exceptions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.database import db_session
from shared.exceptions import MemberNotFound


def _days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _require_member(conn, member_id: str):
    row = conn.execute(
        "SELECT * FROM accounts WHERE member_id = %s", (member_id,)
    ).fetchone()
    if row is None:
        raise MemberNotFound(f"No account for member '{member_id}'.")
    return row


def get_member(member_id: str) -> dict[str, Any]:
    with db_session() as conn:
        row = dict(_require_member(conn, member_id))
        row["balance"] = float(row["balance"])
        row["credit_limit"] = float(row["credit_limit"])
        return row


def get_balance(member_id: str) -> dict[str, Any]:
    with db_session() as conn:
        row = _require_member(conn, member_id)
        return {
            "balance": float(row["balance"]),
            "credit_limit": float(row["credit_limit"]),
            "utilisation": float(row["utilisation"]),
        }


def get_waiver_history(member_id: str, months: int = 8) -> list[dict[str, Any]]:
    with db_session() as conn:
        _require_member(conn, member_id)
        rows = conn.execute(
            "SELECT * FROM waiver_history WHERE member_id = %s AND date >= %s ORDER BY date DESC",
            (member_id, _days_ago_iso(30 * months)),
        ).fetchall()
        return [{**r, "fee_amount": float(r["fee_amount"])} for r in rows]


def get_transactions(member_id: str, limit: int = 10) -> list[dict[str, Any]]:
    with db_session() as conn:
        _require_member(conn, member_id)
        rows = conn.execute(
            "SELECT * FROM transactions WHERE member_id = %s ORDER BY date DESC LIMIT %s",
            (member_id, limit),
        ).fetchall()
        return [{**r, "amount": float(r["amount"])} for r in rows]


def get_cards(member_id: str, active_only: bool = True) -> list[dict[str, Any]]:
    """Every card on the account, masked. Two or more means the graph must ask."""
    with db_session() as conn:
        _require_member(conn, member_id)
        sql = "SELECT * FROM cards WHERE member_id = %s"
        params: list[Any] = [member_id]
        if active_only:
            sql += " AND status = 'active'"
        sql += " ORDER BY is_supplementary, opened_at"
        return conn.execute(sql, params).fetchall()


def get_unreversed_fees(member_id: str, card_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Every unreversed fee, joined to its card for masked display.

    Never returns just the newest: two matches means the graph asks which one.
    Pass `card_id` once the card slot is resolved to narrow the candidates.
    """
    with db_session() as conn:
        _require_member(conn, member_id)
        # Bind the boolean rather than writing FALSE/0: SQLite has no boolean
        # literal and Postgres rejects `= 0`, so only a parameter works on both.
        sql = """SELECT t.*, c.last4, c.product_name
                 FROM transactions t LEFT JOIN cards c ON c.card_id = t.card_id
                 WHERE t.member_id = %s AND t.kind = 'fee' AND t.reversed = %s"""
        params: list[Any] = [member_id, False]
        if card_id:
            sql += " AND t.card_id = %s"
            params.append(card_id)
        sql += " ORDER BY t.date DESC"
        rows = conn.execute(sql, params).fetchall()
        return [{**r, "amount": float(r["amount"])} for r in rows]


def _consecutive_missed(conn, member_id: str) -> int:
    """Unpaid billing periods running back from the most recent.

    Counts the streak, not the total: one missed period two years ago is not
    the same signal as three in a row now.
    """
    rows = conn.execute(
        "SELECT paid FROM payment_history WHERE member_id = %s ORDER BY period DESC",
        (member_id,),
    ).fetchall()
    streak = 0
    for r in rows:
        if r["paid"]:
            break
        streak += 1
    return streak


def _months_since_last_cli(conn, member_id: str) -> Optional[int]:
    """Months since the last credit-limit request, or None if never asked.

    None and 0 mean different things — never asked is not the same as asked
    today — so the policy must distinguish them.
    """
    row = conn.execute(
        """SELECT requested_at FROM cli_history
           WHERE member_id = %s ORDER BY requested_at DESC LIMIT 1""",
        (member_id,),
    ).fetchone()
    if row is None:
        return None
    then = datetime.fromisoformat(row["requested_at"])
    return int((datetime.now(timezone.utc) - then).days // 30)


def get_income_data(member_id: str) -> dict[str, Any]:
    with db_session() as conn:
        row = _require_member(conn, member_id)
        return {"income_staleness_months": row["income_staleness_months"]}


def get_account_facts(
    member_id: str,
    distress_signals: bool = False,
    card_id: Optional[str] = None,
) -> dict[str, Any]:
    """Flatten the account into the fact dict the policy engine consumes.

    Every name a policy YAML references by `field` must appear here, or the rule
    that reads it can never fire.

    `card_id` is optional because it arrives from slot resolution, not from the
    account. Card-scoped facts (`is_primary_holder`, card status, card-level
    fraud hold) are only meaningful once the member has said which card, so they
    stay absent until then rather than being guessed from the first row.
    """
    with db_session() as conn:
        row = _require_member(conn, member_id)
        waivers_8m = conn.execute(
            "SELECT COUNT(*) AS n FROM waiver_history WHERE member_id = %s AND date >= %s",
            (member_id, _days_ago_iso(30 * 8)),
        ).fetchone()["n"]
        replacements_30d = conn.execute(
            """SELECT COUNT(*) AS n FROM transactions
               WHERE member_id = %s AND kind = 'replacement' AND date >= %s""",
            (member_id, _days_ago_iso(30)),
        ).fetchone()["n"]

        facts = {
            "member_id": row["member_id"],
            "tenure_months": row["tenure_months"],
            "late_payments_12m": row["late_payments_12m"],
            "prior_waivers_8m": waivers_8m,
            "income_staleness_months": row["income_staleness_months"],
            "utilisation": float(row["utilisation"]),
            "fraud_hold": row["fraud_hold"],
            "replacements_30d": replacements_30d,
            "distress_signals": distress_signals,
            # Derived rather than stored, so it cannot drift.
            "account_age_days": (
                datetime.now(timezone.utc) - datetime.fromisoformat(row["opened_at"])
            ).days,
            "consecutive_missed_periods": _consecutive_missed(conn, member_id),
            # None means never asked, which is not the same as asked today.
            "months_since_last_cli": _months_since_last_cli(conn, member_id),
            # Compound key for policy dispatch.
            "market": row["market"],
            "product_family": row["product_family"],
        }

        if card_id:
            card = conn.execute(
                "SELECT * FROM cards WHERE card_id = %s AND member_id = %s",
                (card_id, member_id),
            ).fetchone()
            if card is None:
                raise MemberNotFound(f"Card '{card_id}' is not on member '{member_id}'.")
            facts.update({
                "card_id": card["card_id"],
                "card_status": card["status"],
                "card_type": card["card_type"],
                # A supplementary cardmember is not the basic cardmember, and
                # several policies turn on that difference.
                "is_primary_holder": not card["is_supplementary"],
                # Card-level hold beats the account-level flag.
                "fraud_hold": bool(row["fraud_hold"] or card["fraud_hold"]),
            })

        return facts
