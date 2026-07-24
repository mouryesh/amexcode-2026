"""
backend/database.py

Opens the SQLite database and creates the tables. That's it.
Every other backend file calls get_connection() to read/write.

Note: the audit_ledger table is append-only. Nothing here ever writes an
UPDATE or DELETE against it, so history cannot be quietly changed.
"""

import sqlite3

# Where the SQLite file lives. Handoff contract: shared/config.py exposes a
# single `config` object with config.DB_PATH. Until that file exists, default
# to a local file.
try:
    from shared.config import config
    DB_PATH = config.DB_PATH
except ImportError:
    DB_PATH = "servicing.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    member_id      TEXT PRIMARY KEY,
    name           TEXT    NOT NULL,
    email          TEXT,
    phone          TEXT,
    tenure_months  INTEGER NOT NULL,
    balance        REAL    NOT NULL DEFAULT 0,
    credit_limit   REAL    NOT NULL DEFAULT 0,
    utilisation    REAL    NOT NULL DEFAULT 0,
    income_amount  REAL,
    income_asof    TEXT,                       -- ISO date, to spot stale income
    card_status    TEXT    NOT NULL DEFAULT 'active',
    vulnerability  INTEGER NOT NULL DEFAULT 0  -- 0/1 flag
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_ref      TEXT PRIMARY KEY,
    member_id    TEXT    NOT NULL,
    posted_at    TEXT    NOT NULL,
    amount       REAL    NOT NULL,
    description  TEXT    NOT NULL,
    category     TEXT,
    reversed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS waiver_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id      TEXT NOT NULL,
    waived_at      TEXT NOT NULL,
    amount         REAL NOT NULL,
    policy_version TEXT NOT NULL
);

-- Append-only, hash-chained. prev_hash links each row to the one before it.
CREATE TABLE IF NOT EXISTS audit_ledger (
    record_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT,
    timestamp    TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    inputs       TEXT NOT NULL,   -- canonical JSON
    decision     TEXT,            -- canonical JSON, nullable (AuditRecord: dict | None)
    prev_hash    TEXT NOT NULL,
    record_hash  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    action          TEXT NOT NULL,
    inputs_hash     TEXT NOT NULL,
    result          TEXT NOT NULL,   -- canonical JSON of the original result
    created_at      TEXT NOT NULL
);

-- Turn-by-turn transcript: member queries + model replies.
-- Kept separate from audit_ledger so the ledger stays "one row = one decision".
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    member_id   TEXT,
    turn_index  INTEGER NOT NULL,
    role        TEXT    NOT NULL,   -- 'member' | 'agent' | 'system'
    content     TEXT    NOT NULL,
    intent      TEXT,
    confidence  REAL,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, turn_index);
"""


def get_connection():
    """Open a connection. Rows come back dict-like (row["name"])."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create every table if it doesn't already exist. Safe to run repeatedly."""
    conn = get_connection()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def tables_are_empty():
    """True if there are no accounts yet — used to decide whether to seed."""
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    conn.close()
    return count == 0


if __name__ == "__main__":
    init_db()
    print("Database ready at", DB_PATH)
