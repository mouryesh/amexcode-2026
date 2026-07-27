"""backend/database.py — storage engine. PostgreSQL when configured, SQLite otherwise.

One API, two engines, chosen once at import:

    PG_DSN set and psycopg importable  -> PostgreSQL
    otherwise                          -> SQLite

This exists because the audit design must be demonstrable on a laptop with
nothing installed, and deployable on managed Postgres, without two codebases.
Everything above this file — accounts, actions, ledger, the agent, the API —
is written once and does not know which engine it is on.

All caller SQL is written in the Postgres dialect (`%s` placeholders). The
SQLite path rewrites placeholders on the way through, so there is a single
dialect to read and no `if engine ==` scattered through the query code.

The two guarantees this file provides, on both engines:

1. **Serialised chain appends.** `write_transaction()` takes the write lock up
   front — a transaction-scoped advisory lock on Postgres, `BEGIN IMMEDIATE` on
   SQLite. Either way the ledger's read-latest-hash-then-insert cannot
   interleave with another writer, so the chain cannot fork.

2. **Post-commit Splunk shipping.** Audit rows queue during the transaction and
   only ship once it commits. A rollback ships nothing, so the mirror can never
   show an event the database does not have.

Imports: shared.config, shared.splunk.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

from shared import splunk
from shared.config import config

_pending_audit: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar(
    "_pending_audit", default=None
)


# --------------------------------------------------------------------------- #
# Engine selection
# --------------------------------------------------------------------------- #
def _detect_engine() -> str:
    if not config.PG_DSN:
        return "sqlite"
    try:
        import psycopg  # noqa: F401
        import psycopg_pool  # noqa: F401

        return "postgres"
    except ImportError:
        # Configured for Postgres but the driver is absent. Fall back rather
        # than crash — a missing optional dependency should not stop the demo.
        return "sqlite"


ENGINE = _detect_engine()
_pool: Any = None


# --------------------------------------------------------------------------- #
# Schema — one per engine, same logical shape
# --------------------------------------------------------------------------- #
_COMMON_TABLES = """
CREATE TABLE IF NOT EXISTS accounts (
    member_id               TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    tenure_months           INTEGER NOT NULL,
    balance                 {money} NOT NULL,
    credit_limit            {money} NOT NULL,
    late_payments_12m       INTEGER NOT NULL DEFAULT 0,
    income_staleness_months INTEGER NOT NULL DEFAULT 0,
    utilisation             REAL    NOT NULL DEFAULT 0,
    card_id                 TEXT,
    card_status             TEXT    NOT NULL DEFAULT 'active',
    fraud_hold              {bool}  NOT NULL DEFAULT {false},
    vulnerability_flag      {bool}  NOT NULL DEFAULT {false},
    market                  TEXT    NOT NULL DEFAULT 'IN',
    product_family          TEXT    NOT NULL DEFAULT 'personal_credit',
    opened_at               TEXT    NOT NULL,
    email                   TEXT,
    phone                   TEXT
);

CREATE TABLE IF NOT EXISTS cards (
    card_id          TEXT PRIMARY KEY,
    member_id        TEXT NOT NULL REFERENCES accounts(member_id),
    last4            CHAR(4) NOT NULL,
    product_name     TEXT NOT NULL,
    card_type        TEXT NOT NULL DEFAULT 'credit',
    status           TEXT NOT NULL DEFAULT 'active',
    is_supplementary {bool} NOT NULL DEFAULT {false},
    fraud_hold       {bool} NOT NULL DEFAULT {false},
    opened_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_member ON cards (member_id);

CREATE TABLE IF NOT EXISTS transactions (
    txn_ref     TEXT PRIMARY KEY,
    member_id   TEXT NOT NULL REFERENCES accounts(member_id),
    card_id     TEXT REFERENCES cards(card_id),
    date        TEXT NOT NULL,
    amount      {money} NOT NULL,
    description TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'charge',
    reversed    {bool} NOT NULL DEFAULT {false}
);
CREATE INDEX IF NOT EXISTS idx_txn_member ON transactions (member_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_txn_card ON transactions (card_id, date DESC);

CREATE TABLE IF NOT EXISTS waiver_history (
    id             {pk},
    member_id      TEXT NOT NULL REFERENCES accounts(member_id),
    date           TEXT NOT NULL,
    fee_amount     {money} NOT NULL,
    policy_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_waiver_member ON waiver_history (member_id, date DESC);

CREATE TABLE IF NOT EXISTS payment_history (
    id         {pk},
    member_id  TEXT NOT NULL REFERENCES accounts(member_id),
    period     TEXT NOT NULL,
    due_amount {money} NOT NULL,
    paid       {bool} NOT NULL,
    paid_at    TEXT,
    UNIQUE (member_id, period)
);
CREATE INDEX IF NOT EXISTS idx_payhist_member ON payment_history (member_id, period DESC);

CREATE TABLE IF NOT EXISTS cli_history (
    id           {pk},
    member_id    TEXT NOT NULL REFERENCES accounts(member_id),
    requested_at TEXT NOT NULL,
    old_limit    {money},
    new_limit    {money},
    outcome      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clihist_member ON cli_history (member_id, requested_at DESC);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    action      TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chain_anchors (
    id          {pk},
    record_id   BIGINT NOT NULL,
    chain_head  CHAR(64) NOT NULL,
    session_id  TEXT,
    anchored_at TEXT NOT NULL
);
"""

# `inputs` and `decision` are canonical JSON TEXT, not JSONB, because the hash
# covers that exact string. JSONB reorders keys and normalises numbers, which
# would break hash reproducibility on read-back.
_LEDGER = """
CREATE TABLE IF NOT EXISTS audit_ledger (
    record_id    {pk},
    trace_id     TEXT,
    session_id   TEXT,
    member_ref   TEXT,
    event_type   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,
    inputs       TEXT NOT NULL,
    decision     TEXT,
    prev_hash    CHAR(64) NOT NULL,
    record_hash  CHAR(64) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_session ON audit_ledger (session_id, record_id);
CREATE INDEX IF NOT EXISTS idx_ledger_member  ON audit_ledger (member_ref, occurred_at);
CREATE INDEX IF NOT EXISTS idx_ledger_event   ON audit_ledger (event_type, occurred_at);
"""

_PG_EXTRAS = """
ALTER TABLE audit_ledger
    ADD COLUMN IF NOT EXISTS inputs_j JSONB
    GENERATED ALWAYS AS (inputs::jsonb) STORED;

CREATE OR REPLACE FUNCTION audit_ledger_immutable() RETURNS trigger AS $fn$
BEGIN
    RAISE EXCEPTION 'audit_ledger is append-only: % denied on record_id %',
        TG_OP, OLD.record_id;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_ledger_immutable ON audit_ledger;
CREATE TRIGGER trg_audit_ledger_immutable
    BEFORE UPDATE OR DELETE ON audit_ledger
    FOR EACH ROW EXECUTE FUNCTION audit_ledger_immutable();
"""

# Same enforcement on SQLite. Blocks the application and casual SQL; anyone who
# can DROP the trigger can also rewrite the chain, which is why the head is
# anchored externally too.
_SQLITE_EXTRAS = """
CREATE TRIGGER IF NOT EXISTS trg_audit_ledger_no_update
BEFORE UPDATE ON audit_ledger
BEGIN
    SELECT RAISE(ABORT, 'audit_ledger is append-only: UPDATE denied');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_ledger_no_delete
BEFORE DELETE ON audit_ledger
BEGIN
    SELECT RAISE(ABORT, 'audit_ledger is append-only: DELETE denied');
END;
"""

_TYPES = {
    "postgres": {"pk": "BIGSERIAL PRIMARY KEY", "money": "NUMERIC(14,2)",
                 "bool": "BOOLEAN", "false": "FALSE"},
    "sqlite": {"pk": "INTEGER PRIMARY KEY AUTOINCREMENT", "money": "REAL",
               "bool": "INTEGER", "false": "0"},
}


def _schema() -> str:
    """Full DDL for the active engine. Parent tables are declared first."""
    t = _TYPES[ENGINE]
    return (
        _COMMON_TABLES.format(**t)
        + _LEDGER.format(**t)
        + (_PG_EXTRAS if ENGINE == "postgres" else _SQLITE_EXTRAS)
    )


# --------------------------------------------------------------------------- #
# SQLite adapter — same call shape as psycopg
# --------------------------------------------------------------------------- #
_PLACEHOLDER = re.compile(r"%s")


class _SqliteCursorProxy:
    """Wraps sqlite3.Cursor so `conn.execute(...).fetchone()` returns a dict."""

    def __init__(self, cur: sqlite3.Cursor) -> None:
        self._cur = cur

    def fetchone(self):
        row = self._cur.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(r) for r in self._cur.fetchall()]

    def executemany(self, sql: str, seq) -> "_SqliteCursorProxy":
        self._cur.executemany(_PLACEHOLDER.sub("?", sql), seq)
        return self

    @property
    def lastrowid(self):
        return self._cur.lastrowid


class _SqliteConn:
    """Presents the psycopg surface the rest of the codebase is written against."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

    def execute(self, sql: str, params: Any = ()) -> _SqliteCursorProxy:
        return _SqliteCursorProxy(self._raw.execute(_PLACEHOLDER.sub("?", sql), params))

    def executescript(self, sql: str) -> None:
        self._raw.executescript(sql)

    def cursor(self) -> _SqliteCursorProxy:
        return _SqliteCursorProxy(self._raw.cursor())

    @property
    def raw(self) -> sqlite3.Connection:
        return self._raw


def _sqlite_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _get_pool():
    global _pool
    if _pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(
            conninfo=config.PG_DSN,
            min_size=config.PG_POOL_MIN,
            max_size=config.PG_POOL_MAX,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


# --------------------------------------------------------------------------- #
# Public API — identical on both engines
# --------------------------------------------------------------------------- #
@contextmanager
def db_session() -> Iterator[Any]:
    """Read or standalone-write connection. Commits on success, rolls back on error.

    Does NOT hold the chain lock — anything appending to the ledger must use
    `write_transaction()`.
    """
    if ENGINE == "postgres":
        with _get_pool().connection() as conn:
            yield conn
        return

    raw = _sqlite_connect()
    try:
        yield _SqliteConn(raw)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


@contextmanager
def write_transaction() -> Iterator[Any]:
    """One serialised, all-or-nothing write, with the chain lock held up front."""
    token = _pending_audit.set([])
    try:
        if ENGINE == "postgres":
            with _get_pool().connection() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (config.LEDGER_LOCK_KEY,))
                yield conn
        else:
            raw = _sqlite_connect()
            raw.isolation_level = None  # manual BEGIN/COMMIT
            try:
                raw.execute("BEGIN IMMEDIATE")  # write lock before any read
                yield _SqliteConn(raw)
                raw.execute("COMMIT")
            except Exception:
                raw.execute("ROLLBACK")
                raise
            finally:
                raw.close()

        events = _pending_audit.get() or []
        if events:
            splunk.ship_many(events)
    finally:
        _pending_audit.reset(token)


def queue_audit_event(event: dict[str, Any]) -> None:
    """Hold an audit event for post-commit shipping; ship now if not in a transaction."""
    pending = _pending_audit.get()
    if pending is None:
        splunk.ship_many([event])
    else:
        pending.append(event)


def init_db() -> None:
    """Create tables, indexes and the immutability trigger. Safe to re-run."""
    if ENGINE == "postgres":
        with _get_pool().connection() as conn:
            conn.execute(_schema())
    else:
        raw = _sqlite_connect()
        try:
            raw.executescript(_schema())
            raw.commit()
        finally:
            raw.close()


def tables_empty() -> bool:
    with db_session() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"] == 0


def ping() -> bool:
    try:
        with db_session() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


@contextmanager
def ledger_guard_disabled() -> Iterator[None]:
    """Temporarily lift the append-only trigger. Tamper demonstrations only.

    Having to do this at all is the demonstration: the guard stops the
    application and casual SQL, and the hash chain then catches whoever lifts
    it. Never call this from application code.
    """
    with db_session() as conn:
        if ENGINE == "postgres":
            conn.execute(
                "ALTER TABLE audit_ledger DISABLE TRIGGER trg_audit_ledger_immutable"
            )
        else:
            conn.execute("DROP TRIGGER IF EXISTS trg_audit_ledger_no_update")
            conn.execute("DROP TRIGGER IF EXISTS trg_audit_ledger_no_delete")
    try:
        yield
    finally:
        with db_session() as conn:
            if ENGINE == "postgres":
                conn.execute(
                    "ALTER TABLE audit_ledger ENABLE TRIGGER trg_audit_ledger_immutable"
                )
            else:
                conn.executescript(_SQLITE_EXTRAS)


def engine_info() -> dict[str, Any]:
    """What the health endpoint and the UI banner report."""
    return {
        "engine": ENGINE,
        "target": config.PG_DSN if ENGINE == "postgres" else config.DB_PATH,
        "splunk": config.splunk_configured(),
    }
