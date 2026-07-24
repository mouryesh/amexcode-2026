"""tests/test_ledger.py — the tamper-and-go-red proof.

Uses a temp DB via the DB_PATH env var (set by conftest) so tests never touch
the demo database.
"""
from backend import ledger
from backend.database import db_session, init_db


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
