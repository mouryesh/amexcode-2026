"""backend/database.py — SQLite engine, connections, and table DDL.

Owned by Person B. The append-only constraint on `audit_ledger` is enforced
here at the access layer: no function anywhere constructs an UPDATE or DELETE
against it. There is deliberately no such code path in this file.

Imports: shared.config only.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from shared.config import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    member_id             TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    tenure_months         INTEGER NOT NULL,
    balance               REAL NOT NULL,
    credit_limit          REAL NOT NULL,
    late_payments_12m     INTEGER NOT NULL DEFAULT 0,
    income_staleness_months INTEGER NOT NULL DEFAULT 0,
    utilisation           REAL NOT NULL DEFAULT 0.0,
    card_id               TEXT,
    card_status           TEXT NOT NULL DEFAULT 'active',
    fraud_hold            INTEGER NOT NULL DEFAULT 0,
    vulnerability_flag    INTEGER NOT NULL DEFAULT 0,
    email                 TEXT,
    phone                 TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_ref     TEXT PRIMARY KEY,
    member_id   TEXT NOT NULL,
    date        TEXT NOT NULL,
    amount      REAL NOT NULL,
    description TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'charge',
    reversed    INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (member_id) REFERENCES accounts(member_id)
);

CREATE TABLE IF NOT EXISTS waiver_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id      TEXT NOT NULL,
    date           TEXT NOT NULL,
    fee_amount     REAL NOT NULL,
    policy_version TEXT NOT NULL,
    FOREIGN KEY (member_id) REFERENCES accounts(member_id)
);

-- Append-only, hash-chained. No UPDATE or DELETE path exists for this table.
CREATE TABLE IF NOT EXISTS audit_ledger (
    record_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    timestamp   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    inputs      TEXT NOT NULL,
    decision    TEXT,
    prev_hash   TEXT NOT NULL,
    record_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    inputs_hash TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    """Open a connection with row access by column name and FKs enforced."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    """Transactional connection context manager: commit on success, rollback on error."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they do not exist. Safe to call repeatedly."""
    with db_session() as conn:
        conn.executescript(_SCHEMA)


def tables_empty() -> bool:
    """True when there are no accounts yet — used to gate seeding on startup."""
    with db_session() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()
        return row["n"] == 0
