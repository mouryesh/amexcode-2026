"""backend/seed.py — idempotent seeding of the four demo personas.

Re-runnable: you will nuke the database during demo prep and need it back in
seconds. Persona rows are replaced so their facts always match the script.
The audit ledger and idempotency table are deliberately left alone — wiping the
chain to reseed would defeat the point of having one.

    Priya   — 3yr tenure, no late payments, no prior waivers   -> APPROVE
    Rahul   — two courtesy waivers in the last 8 months        -> DECLINE
    Ananya  — 6 months tenure, income data 14 months stale     -> QUEUE
    Vikram  — rising utilisation, mentions job loss            -> ESCALATE

Imports: backend.database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.database import db_session, init_db


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


_ACCOUNTS = [
    # member_id, name, tenure, balance, limit, late_12m, income_stale,
    # utilisation, card_id, fraud_hold, market, product_family, email, phone
    ("MEM-PRIYA", "Priya Sharma", 36, 12500.0, 150000.0, 0, 2, 0.08, "CARD-P1", False,
     "IN", "personal_credit", "priya@example.com", "+91-90000-00001"),
    ("MEM-RAHUL", "Rahul Verma", 28, 22000.0, 120000.0, 1, 3, 0.18, "CARD-R1", False,
     "IN", "personal_credit", "rahul@example.com", "+91-90000-00002"),
    ("MEM-ANANYA", "Ananya Rao", 6, 41000.0, 60000.0, 0, 14, 0.68, "CARD-A1", False,
     "IN", "personal_credit", "ananya@example.com", "+91-90000-00003"),
    ("MEM-VIKRAM", "Vikram Singh", 48, 88000.0, 100000.0, 2, 4, 0.88, "CARD-V1", False,
     "IN", "personal_credit", "vikram@example.com", "+91-90000-00004"),
]

_TXNS = [
    ("TXN-PRIYA-FEE", "MEM-PRIYA", _days_ago(3), 500.0, "Late payment fee", "fee"),
    ("TXN-RAHUL-FEE", "MEM-RAHUL", _days_ago(2), 500.0, "Late payment fee", "fee"),
    ("TXN-ANANYA-FEE", "MEM-ANANYA", _days_ago(5), 500.0, "Late payment fee", "fee"),
    ("TXN-VIKRAM-FEE", "MEM-VIKRAM", _days_ago(4), 500.0, "Late payment fee", "fee"),
]

# Rahul already has two courtesy waivers inside the 8-month window -> DECLINE.
_WAIVERS = [
    ("MEM-RAHUL", _days_ago(60), 500.0, "v1"),
    ("MEM-RAHUL", _days_ago(150), 500.0, "v1"),
]


def seed() -> None:
    init_db()
    members = tuple(a[0] for a in _ACCOUNTS)
    with db_session() as conn:
        conn.execute("DELETE FROM waiver_history WHERE member_id = ANY(%s)", (list(members),))
        conn.execute("DELETE FROM transactions WHERE member_id = ANY(%s)", (list(members),))
        conn.execute("DELETE FROM accounts WHERE member_id = ANY(%s)", (list(members),))

        conn.cursor().executemany(
            """INSERT INTO accounts
               (member_id, name, tenure_months, balance, credit_limit, late_payments_12m,
                income_staleness_months, utilisation, card_id, fraud_hold,
                market, product_family, email, phone)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            _ACCOUNTS,
        )
        conn.cursor().executemany(
            """INSERT INTO transactions (txn_ref, member_id, date, amount, description, kind)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            _TXNS,
        )
        conn.cursor().executemany(
            """INSERT INTO waiver_history (member_id, date, fee_amount, policy_version)
               VALUES (%s,%s,%s,%s)""",
            _WAIVERS,
        )


if __name__ == "__main__":
    seed()
    print("Seeded 4 personas: Priya, Rahul, Ananya, Vikram.")
