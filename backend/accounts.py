"""
backend/accounts.py

Read-side. Pulls facts out of the DB for the policy engine and tools.
Read-only: no function here changes anything.
"""

from datetime import datetime, timedelta, timezone

from backend.database import get_connection
from shared.exceptions import MemberNotFound


def get_member(member_id):
    """Full account profile. Raises MemberNotFound if no such member."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM accounts WHERE member_id = ?", (member_id,)
    ).fetchone()
    conn.close()
    if row is None:
        raise MemberNotFound(member_id)
    return dict(row)


def get_balance(member_id):
    """Balance, credit limit, and utilisation."""
    conn = get_connection()
    row = conn.execute(
        "SELECT balance, credit_limit, utilisation FROM accounts WHERE member_id = ?",
        (member_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_waiver_history(member_id, months=12):
    """Courtesy waivers within the last `months`, newest first."""
    # ponytail: ~30 days/month is close enough for policy windows; swap for
    # dateutil.relativedelta if exact calendar months ever matter.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30 * months)).isoformat()
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM waiver_history WHERE member_id = ? AND waived_at >= ?"
        " ORDER BY waived_at DESC",
        (member_id, cutoff),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_transactions(member_id, limit=10):
    """Recent charges, newest first."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM transactions WHERE member_id = ? ORDER BY posted_at DESC"
        " LIMIT ?",
        (member_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_income_data(member_id):
    """Income amount, the date it was recorded, and how many months stale."""
    conn = get_connection()
    row = conn.execute(
        "SELECT income_amount, income_asof FROM accounts WHERE member_id = ?",
        (member_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None

    data = dict(row)
    data["months_stale"] = None
    if data["income_asof"]:
        asof = datetime.fromisoformat(data["income_asof"])
        if asof.tzinfo is None:
            asof = asof.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - asof).days
        data["months_stale"] = days // 30
    return data


if __name__ == "__main__":
    # Self-check: seed one member with a recent + an old waiver, read back.
    import os

    import backend.database as db

    db.DB_PATH = "_accounts_selfcheck.db"
    if os.path.exists(db.DB_PATH):
        os.remove(db.DB_PATH)
    db.init_db()

    recent = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()

    conn = get_connection()
    conn.execute("INSERT INTO accounts (member_id, name, tenure_months, balance,"
                 " credit_limit, income_amount, income_asof) VALUES"
                 " ('M1', 'Ananya', 6, 200, 5000, 60000, ?)", (old,))
    conn.execute("INSERT INTO transactions (txn_ref, member_id, posted_at, amount,"
                 " description) VALUES ('TXN-1', 'M1', '2026-06-01', 500, 'late fee')")
    conn.execute("INSERT INTO waiver_history (member_id, waived_at, amount,"
                 " policy_version) VALUES ('M1', ?, 500, 'v1')", (recent,))
    conn.execute("INSERT INTO waiver_history (member_id, waived_at, amount,"
                 " policy_version) VALUES ('M1', ?, 500, 'v1')", (old,))
    conn.commit()
    conn.close()

    assert get_member("M1")["name"] == "Ananya"
    try:
        get_member("NOPE")
        assert False, "missing member must raise"
    except MemberNotFound:
        pass
    assert get_balance("M1")["credit_limit"] == 5000
    assert len(get_transactions("M1")) == 1
    # 6-month window catches the recent waiver, not the 400-day-old one.
    assert len(get_waiver_history("M1", months=6)) == 1
    assert len(get_waiver_history("M1", months=24)) == 2
    assert get_income_data("M1")["months_stale"] >= 13

    os.remove(db.DB_PATH)
    print("accounts self-check passed.")
