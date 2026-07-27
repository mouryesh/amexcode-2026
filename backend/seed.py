"""backend/seed.py — idempotent seeding of the demo personas.

Re-runnable: you will nuke the database during demo prep and need it back in
seconds. Persona rows are replaced so their facts always match the script. The
audit ledger and idempotency table are deliberately left alone — wiping the
chain to reseed would defeat the point of having one.

    Priya   — 3yr tenure, clean history, TWO cards         -> APPROVE, after
                                                             a card question
    Rahul   — two courtesy waivers in the last 8 months    -> DECLINE
    Ananya  — 6 months tenure, income data 14 months stale -> QUEUE
    Vikram  — rising utilisation, three missed periods     -> ESCALATE

Priya carries two cards on purpose: with one card the disambiguation branch is
unreachable, and an untested branch is an unbuilt branch.

Imports: backend.database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend import docstore
from backend.database import db_session, init_db


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _months_ago(n: int) -> str:
    return _days_ago(30 * n)


def _period(n_ago: int) -> str:
    d = datetime.now(timezone.utc) - timedelta(days=30 * n_ago)
    return d.strftime("%Y-%m")


# member_id, name, tenure, balance, limit, late_12m, income_stale, utilisation,
# card_id, fraud_hold, market, product_family, opened_at, email, phone
_ACCOUNTS = [
    ("MEM-PRIYA", "Priya Sharma", 36, 12500.0, 150000.0, 0, 2, 0.08, "CARD-P1", False,
     "IN", "personal_credit", _months_ago(36), "priya@example.com", "+91-90000-00001"),
    ("MEM-RAHUL", "Rahul Verma", 28, 22000.0, 120000.0, 1, 3, 0.18, "CARD-R1", False,
     "IN", "personal_credit", _months_ago(28), "rahul@example.com", "+91-90000-00002"),
    ("MEM-ANANYA", "Ananya Rao", 6, 41000.0, 60000.0, 0, 14, 0.68, "CARD-A1", False,
     "IN", "personal_credit", _months_ago(6), "ananya@example.com", "+91-90000-00003"),
    ("MEM-VIKRAM", "Vikram Singh", 48, 88000.0, 100000.0, 2, 4, 0.88, "CARD-V1", False,
     "IN", "personal_credit", _months_ago(48), "vikram@example.com", "+91-90000-00004"),
    # Deepa exists to carry the multi-card case. Priya deliberately keeps ONE
    # card and ONE fee so the single-ask happy path stays a happy path — an
    # ambiguity question on the headline demo would look like a regression.
    ("MEM-DEEPA", "Deepa Iyer", 30, 33000.0, 200000.0, 0, 3, 0.16, "CARD-D1", False,
     "IN", "personal_credit", _months_ago(30), "deepa@example.com", "+91-90000-00005"),
]

# card_id, member_id, last4, product_name, card_type, status, is_supplementary,
# fraud_hold, opened_at
_CARDS = [
    ("CARD-P1", "MEM-PRIYA", "1007", "Amex Gold Charge", "charge", "active", False,
     False, _months_ago(36)),
    ("CARD-R1", "MEM-RAHUL", "2210", "Amex Membership Rewards", "credit", "active", False,
     False, _months_ago(28)),
    ("CARD-A1", "MEM-ANANYA", "7781", "Amex SmartEarn", "credit", "active", False,
     False, _months_ago(6)),
    ("CARD-V1", "MEM-VIKRAM", "3095", "Amex Membership Rewards", "credit", "active", False,
     False, _months_ago(48)),
    # Deepa holds two -> the disambiguation branch is reachable.
    ("CARD-D1", "MEM-DEEPA", "5510", "Amex Platinum Travel", "credit", "active", False,
     False, _months_ago(30)),
    ("CARD-D2", "MEM-DEEPA", "8823", "Amex SmartEarn", "credit", "active", False,
     False, _months_ago(9)),
]

# One fee each, except Deepa who has one per card -> two candidates -> ask.
# txn_ref, member_id, card_id, date, amount, description, kind
_TXNS = [
    ("TXN-PRIYA-FEE", "MEM-PRIYA", "CARD-P1", _days_ago(3), 500.0,
     "Late payment fee", "fee"),
    ("TXN-RAHUL-FEE", "MEM-RAHUL", "CARD-R1", _days_ago(2), 500.0,
     "Late payment fee", "fee"),
    ("TXN-ANANYA-FEE", "MEM-ANANYA", "CARD-A1", _days_ago(5), 500.0,
     "Late payment fee", "fee"),
    ("TXN-VIKRAM-FEE", "MEM-VIKRAM", "CARD-V1", _days_ago(4), 500.0,
     "Late payment fee", "fee"),
    ("TXN-DEEPA-FEE-1", "MEM-DEEPA", "CARD-D1", _days_ago(4), 500.0,
     "Late payment fee", "fee"),
    ("TXN-DEEPA-FEE-2", "MEM-DEEPA", "CARD-D2", _days_ago(9), 500.0,
     "Late payment fee", "fee"),
]

# Rahul already has two courtesy waivers inside the 8-month window -> DECLINE.
_WAIVERS = [
    ("MEM-RAHUL", _days_ago(60), 500.0, "v1"),
    ("MEM-RAHUL", _days_ago(150), 500.0, "v1"),
]

# Vikram: three unpaid periods running -> consecutive_missed_periods = 3.
# Everyone else is current, so their streak is 0.
_PAYMENTS = (
    [("MEM-PRIYA", _period(i), 12500.0, True, _period(i)) for i in range(1, 7)]
    + [("MEM-RAHUL", _period(i), 22000.0, True, _period(i)) for i in range(1, 7)]
    + [("MEM-ANANYA", _period(i), 41000.0, True, _period(i)) for i in range(1, 7)]
    + [("MEM-DEEPA", _period(i), 33000.0, True, _period(i)) for i in range(1, 7)]
    + [("MEM-VIKRAM", _period(i), 88000.0, False, None) for i in range(1, 4)]
    + [("MEM-VIKRAM", _period(i), 88000.0, True, _period(i)) for i in range(4, 7)]
)

# Ananya asked for a limit increase two months ago and was queued.
_CLI = [
    ("MEM-ANANYA", _months_ago(2), 60000.0, None, "QUEUE"),
]


def seed() -> None:
    init_db()
    docstore.init()
    members = [a[0] for a in _ACCOUNTS]
    # `IN (%s,%s,...)` rather than Postgres `ANY(%s)`: the same SQL has to run
    # on SQLite, which has no array type.
    marks = ",".join(["%s"] * len(members))
    with db_session() as conn:
        # Children before parents, or the foreign keys reject the delete.
        for table in ("cli_history", "payment_history", "waiver_history",
                      "transactions", "cards", "accounts"):
            conn.execute(f"DELETE FROM {table} WHERE member_id IN ({marks})", members)

        cur = conn.cursor()
        cur.executemany(
            """INSERT INTO accounts
               (member_id, name, tenure_months, balance, credit_limit, late_payments_12m,
                income_staleness_months, utilisation, card_id, fraud_hold,
                market, product_family, opened_at, email, phone)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            _ACCOUNTS,
        )
        cur.executemany(
            """INSERT INTO cards
               (card_id, member_id, last4, product_name, card_type, status,
                is_supplementary, fraud_hold, opened_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            _CARDS,
        )
        cur.executemany(
            """INSERT INTO transactions
               (txn_ref, member_id, card_id, date, amount, description, kind)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            _TXNS,
        )
        cur.executemany(
            """INSERT INTO waiver_history (member_id, date, fee_amount, policy_version)
               VALUES (%s,%s,%s,%s)""",
            _WAIVERS,
        )
        cur.executemany(
            """INSERT INTO payment_history (member_id, period, due_amount, paid, paid_at)
               VALUES (%s,%s,%s,%s,%s)""",
            _PAYMENTS,
        )
        cur.executemany(
            """INSERT INTO cli_history (member_id, requested_at, old_limit, new_limit, outcome)
               VALUES (%s,%s,%s,%s,%s)""",
            _CLI,
        )


if __name__ == "__main__":
    seed()
    print("Seeded 4 personas, 5 cards, 5 fees, 24 billing periods.")
    print("Priya holds two cards with a fee on each — the disambiguation case.")
