"""backend/seed.py — idempotent seeding of the four demo personas.

Owned by Person B. Runnable repeatedly: you will nuke the database during demo
prep and need to reseed in seconds. Re-running replaces persona rows so their
facts always match the demo script.

    Priya   — 3yr tenure, zero late payments, no prior waivers   → APPROVE
    Rahul   — two courtesy waivers in the last 8 months          → DECLINE
    Ananya  — 6 months tenure, income data 14 months stale       → QUEUE
    Vikram  — rising utilisation, mentions job loss              → ESCALATE

Imports: backend.database, backend.accounts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.database import db_session, init_db


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


_ACCOUNTS = [
    # member_id, name, tenure_months, balance, credit_limit, late_12m,
    # income_stale_months, utilisation, card_id, fraud_hold, email, phone
    ("MEM-PRIYA", "Priya Sharma", 36, 12500.0, 150000.0, 0, 2, 0.08, "CARD-P1", 0,
     "priya@example.com", "+91-90000-00001"),
    ("MEM-RAHUL", "Rahul Verma", 28, 22000.0, 120000.0, 1, 3, 0.18, "CARD-R1", 0,
     "rahul@example.com", "+91-90000-00002"),
    ("MEM-ANANYA", "Ananya Rao", 6, 41000.0, 60000.0, 0, 14, 0.68, "CARD-A1", 0,
     "ananya@example.com", "+91-90000-00003"),
    ("MEM-VIKRAM", "Vikram Singh", 48, 88000.0, 100000.0, 2, 4, 0.88, "CARD-V1", 0,
     "vikram@example.com", "+91-90000-00004"),
]

# Recent late-fee charge each persona can ask to have waived.
_TXNS = [
    ("TXN-PRIYA-FEE", "MEM-PRIYA", _days_ago(3), 500.0, "Late payment fee", "fee"),
    ("TXN-RAHUL-FEE", "MEM-RAHUL", _days_ago(2), 500.0, "Late payment fee", "fee"),
    ("TXN-ANANYA-FEE", "MEM-ANANYA", _days_ago(5), 500.0, "Late payment fee", "fee"),
    ("TXN-VIKRAM-FEE", "MEM-VIKRAM", _days_ago(4), 500.0, "Late payment fee", "fee"),
]

# Rahul already has two courtesy waivers inside the 8-month window → DECLINE.
_WAIVERS = [
    ("MEM-RAHUL", _days_ago(60), 500.0, "v1"),
    ("MEM-RAHUL", _days_ago(150), 500.0, "v1"),
]


def seed() -> None:
    """Create tables if needed, then upsert the four personas and their history."""
    init_db()
    with db_session() as conn:
        # Wipe persona-scoped rows so reseeding is deterministic. The audit
        # ledger and idempotency table are intentionally left untouched.
        members = tuple(a[0] for a in _ACCOUNTS)
        placeholders = ",".join("?" for _ in members)
        conn.execute(f"DELETE FROM waiver_history WHERE member_id IN ({placeholders})", members)
        conn.execute(f"DELETE FROM transactions WHERE member_id IN ({placeholders})", members)
        conn.execute(f"DELETE FROM accounts WHERE member_id IN ({placeholders})", members)

        conn.executemany(
            """INSERT INTO accounts
               (member_id, name, tenure_months, balance, credit_limit, late_payments_12m,
                income_staleness_months, utilisation, card_id, fraud_hold, email, phone)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            _ACCOUNTS,
        )
        conn.executemany(
            """INSERT INTO transactions (txn_ref, member_id, date, amount, description, kind)
               VALUES (?,?,?,?,?,?)""",
            _TXNS,
        )
        conn.executemany(
            """INSERT INTO waiver_history (member_id, date, fee_amount, policy_version)
               VALUES (?,?,?,?)""",
            _WAIVERS,
        )


if __name__ == "__main__":
    seed()
    print("Seeded 4 personas: Priya, Rahul, Ananya, Vikram.")
