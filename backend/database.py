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

-- Turn-by-turn transcript, per session. Kept separate from audit_ledger so the
-- ledger stays "one row = one decision". Lets the agent thread prior turns into
-- a fresh AgentState (real transcript, real cross-turn sentiment) and detect
-- repeated-request patterns (e.g. the same decline asked again).
--
-- RECONCILED SCHEMA (see BACKEND_RECONCILIATION.md): a superset of the two
-- parallel conversation tables that existed on the mouryesh and Yash_Amex
-- branches. Adopts the Yash_Amex column names (turn_index, role, content,
-- intent, confidence, created_at) so both people's code writes the same shape,
-- and keeps the decision-tracking columns the repeat-decline escalation needs
-- (decision_outcome, decision_reason_code, policy_id).
CREATE TABLE IF NOT EXISTS messages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id           TEXT    NOT NULL,
    member_id            TEXT,
    turn_index           INTEGER NOT NULL,
    role                 TEXT    NOT NULL,   -- 'member' | 'agent' | 'system'
    content              TEXT    NOT NULL,
    intent               TEXT,
    confidence           REAL,
    decision_outcome     TEXT,
    decision_reason_code TEXT,
    policy_id            TEXT,
    created_at           TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, turn_index);
"""


def get_connection() -> sqlite3.Connection:
    """Open a connection with row access by column name and FKs enforced."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers run concurrently with the single writer; busy_timeout
    # makes a second writer WAIT for the lock (up to 5s) instead of immediately
    # raising "database is locked". Together with write_transaction()'s
    # BEGIN IMMEDIATE, this serialises writes cleanly under concurrency.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    """Transactional connection context manager: commit on success, rollback on error.

    Used for reads and standalone writes. For the atomic action path (mutate +
    ledger append + idempotency, all-or-nothing, serialised) use
    write_transaction() instead.
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_transaction() -> Iterator[sqlite3.Connection]:
    """A single serialised, all-or-nothing write transaction.

    Issues BEGIN IMMEDIATE, which acquires the write lock UP FRONT — before any
    read in the transaction body. This is what makes the ledger's
    read-latest-hash-then-insert safe under concurrency: no other writer can
    slip an append in between, so the hash chain can never fork. Everything in
    the body commits together or rolls back together, so an account mutation,
    its ledger row, and its idempotency record are never left partially written.
    """
    conn = get_connection()
    conn.isolation_level = None  # take manual control of BEGIN/COMMIT/ROLLBACK
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
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
