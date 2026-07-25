"""tests/test_ledger.py — the tamper-and-go-red proof.

Uses a temp DB via the DB_PATH env var (set by conftest) so tests never touch
the demo database.
"""
import threading

from backend import actions, ledger
from backend.database import db_session, init_db
from shared.exceptions import IdempotencyConflict


def _fresh_db():
    init_db()
    with db_session() as conn:
        conn.execute("DELETE FROM audit_ledger")
        # Reset AUTOINCREMENT so record ids restart at 1 for each test.
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'audit_ledger'")


def test_clean_chain_verifies():
    _fresh_db()
    ledger.append("system", "reverse_fee", {"amount": 500}, {"outcome": "APPROVE"}, "s1")
    ledger.append("system", "block_card", {"card_id": "C1"}, None, "s1")
    ledger.append("system", "issue_replacement", {"card_id": "C1"}, None, "s1")

    result = ledger.verify()
    assert result["status"] == "OK"
    assert result["records"] == 3


def test_tampered_chain_identifies_record():
    _fresh_db()
    ledger.append("system", "a", {"x": 1}, None, "s2")
    ledger.append("system", "b", {"x": 2}, None, "s2")
    ledger.append("system", "c", {"x": 3}, None, "s2")

    # Tamper with the second record's inputs directly in the DB.
    with db_session() as conn:
        conn.execute("UPDATE audit_ledger SET inputs = ? WHERE record_id = 2",
                     ('{"x":999}',))

    result = ledger.verify()
    assert result["status"] == "TAMPERED"
    assert result["broken_at_record_id"] == 2


def test_appends_chain_forward():
    _fresh_db()
    r1 = ledger.append("system", "a", {"x": 1}, None, "s3")
    r2 = ledger.append("system", "b", {"x": 2}, None, "s3")
    # Each record's prev_hash is its predecessor's record_hash.
    assert r2["prev_hash"] == r1["record_hash"]
    # Same-content records still differ because of timestamp + chaining.
    r3 = ledger.append("system", "a", {"x": 1}, None, "s3")
    assert r3["record_hash"] != r1["record_hash"]


def _fresh_accounts_db():
    """Reset ledger + a seeded member so concurrent write actions have a target."""
    init_db()
    with db_session() as conn:
        conn.execute("DELETE FROM audit_ledger")
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'audit_ledger'")
        conn.execute("DELETE FROM idempotency_keys")
        conn.execute("DELETE FROM accounts WHERE member_id = 'MEM-CONC'")
        conn.execute(
            "INSERT INTO accounts (member_id, name, tenure_months, balance, credit_limit)"
            " VALUES ('MEM-CONC', 'Concurrency', 24, 100000, 100000)"
        )


def test_concurrent_appends_do_not_fork_the_chain():
    """The headline: many writers hammering the ledger at once keep it intact.

    Before the fix, ledger.append() read the latest hash then inserted on a
    separate transaction — two concurrent appends could read the same prev_hash
    and fork the chain, making verify() go red on legitimate traffic. With
    BEGIN IMMEDIATE serialising the read-then-insert, every record chains
    cleanly no matter the interleaving.
    """
    _fresh_db()
    threads_count, per_thread = 8, 15

    def worker(t: int):
        for i in range(per_thread):
            ledger.append("system", "concurrent", {"t": t, "i": i}, None, "sC")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    result = ledger.verify()
    assert result["status"] == "OK", result
    assert result["records"] == threads_count * per_thread


def test_concurrent_write_actions_keep_ledger_and_balance_consistent():
    """End-to-end: concurrent reverse_fee calls with DISTINCT idempotency keys.

    Every successful action must leave exactly one ledger row and one balance
    decrement — no lost/duplicated ledger rows, chain still verifies.
    """
    _fresh_accounts_db()
    n = 20

    def worker(k: int):
        actions.reverse_fee("MEM-CONC", 10, "TXN-X", {"outcome": "APPROVE"},
                            idempotency_key=f"conc-{k}")

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert ledger.verify()["status"] == "OK"
    with db_session() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_ledger WHERE action = 'reverse_fee'"
        ).fetchone()["n"]
        balance = conn.execute(
            "SELECT balance FROM accounts WHERE member_id = 'MEM-CONC'"
        ).fetchone()["balance"]
    assert rows == n, f"expected {n} ledger rows, got {rows}"
    assert balance == 100000 - n * 10, balance


def test_conflicting_action_writes_nothing_atomicity():
    """A failed action leaves NO partial state — no mutation, no ledger row.

    Reusing an idempotency key with different inputs raises IdempotencyConflict.
    Because the whole action is one transaction, the conflicting call must not
    have mutated the balance or appended a ledger row.
    """
    _fresh_accounts_db()
    actions.reverse_fee("MEM-CONC", 500, "TXN-A", {"outcome": "APPROVE"},
                        idempotency_key="dup")

    with db_session() as conn:
        ledger_rows_before = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_ledger"
        ).fetchone()["n"]
        balance_before = conn.execute(
            "SELECT balance FROM accounts WHERE member_id = 'MEM-CONC'"
        ).fetchone()["balance"]

    # Same key, different inputs → conflict, must be fully rolled back.
    try:
        actions.reverse_fee("MEM-CONC", 999, "TXN-B", {"outcome": "APPROVE"},
                            idempotency_key="dup")
        assert False, "expected IdempotencyConflict"
    except IdempotencyConflict:
        pass

    with db_session() as conn:
        ledger_rows_after = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_ledger"
        ).fetchone()["n"]
        balance_after = conn.execute(
            "SELECT balance FROM accounts WHERE member_id = 'MEM-CONC'"
        ).fetchone()["balance"]

    assert ledger_rows_after == ledger_rows_before, "conflict must not append a ledger row"
    assert balance_after == balance_before, "conflict must not mutate the balance"
