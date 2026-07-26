"""backend/database.py — PostgreSQL engine, pooling, DDL, transactions.

Replaces the SQLite engine. The public surface is deliberately unchanged
(`db_session`, `write_transaction`, `init_db`, `tables_empty`) so the agent
layer needed no edits — only the SQL placeholder style moved from `?` to `%s`.

Two guarantees this file exists to provide:

1. **Serialised chain appends.** `write_transaction()` takes a Postgres
   *transaction-scoped advisory lock* before anything else runs. That is the
   direct translation of SQLite's `BEGIN IMMEDIATE`: the write lock is held from
   the top of the transaction, so the ledger's read-latest-hash-then-insert can
   never interleave with another writer and the chain cannot fork. The lock is
   released automatically at COMMIT or ROLLBACK — there is no unlock path to
   forget.

2. **Post-commit Splunk shipping.** Audit rows are queued during the transaction
   and only handed to Splunk *after* the commit succeeds. A rolled-back
   transaction ships nothing, so Splunk can never show an event the database
   does not have.

Throughput note: the advisory lock serialises every write action, not just the
ledger insert, because they share one transaction. That is the correct trade for
an audit chain — correctness over concurrency — but it does cap write throughput
at one action at a time. Sharding the chain by member would lift that, at the
cost of one chain head per shard to anchor.

Imports: shared.config, shared.splunk.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from shared import splunk
from shared.config import config

# Audit events accumulated during the current write transaction, shipped to
# Splunk only once that transaction commits.
_pending_audit: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar(
    "_pending_audit", default=None
)

_pool: Optional[ConnectionPool] = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    member_id               TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    tenure_months           INTEGER NOT NULL,
    balance                 NUMERIC(14,2) NOT NULL,
    credit_limit            NUMERIC(14,2) NOT NULL,
    late_payments_12m       INTEGER NOT NULL DEFAULT 0,
    income_staleness_months INTEGER NOT NULL DEFAULT 0,
    utilisation             REAL    NOT NULL DEFAULT 0,
    card_id                 TEXT,
    card_status             TEXT    NOT NULL DEFAULT 'active',
    fraud_hold              BOOLEAN NOT NULL DEFAULT FALSE,
    vulnerability_flag      BOOLEAN NOT NULL DEFAULT FALSE,
    -- Compound key for policy dispatch: the same intent resolves to a
    -- different policy on a charge card, or in another market.
    market                  TEXT    NOT NULL DEFAULT 'IN',
    product_family          TEXT    NOT NULL DEFAULT 'personal_credit',
    email                   TEXT,
    phone                   TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_ref     TEXT PRIMARY KEY,
    member_id   TEXT NOT NULL REFERENCES accounts(member_id),
    date        TEXT NOT NULL,
    amount      NUMERIC(14,2) NOT NULL,
    description TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'charge',
    reversed    BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_txn_member ON transactions (member_id, date DESC);

CREATE TABLE IF NOT EXISTS waiver_history (
    id             BIGSERIAL PRIMARY KEY,
    member_id      TEXT NOT NULL REFERENCES accounts(member_id),
    date           TEXT NOT NULL,
    fee_amount     NUMERIC(14,2) NOT NULL,
    policy_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_waiver_member ON waiver_history (member_id, date DESC);

-- Append-only, hash-chained.
--
-- `inputs` and `decision` are stored as canonical JSON TEXT, not JSONB, because
-- the hash is computed over that exact string. JSONB normalises key order and
-- numeric representation, which would silently break hash reproducibility on
-- read-back. The generated JSONB columns alongside give indexing and search
-- without touching the bytes that were hashed.
CREATE TABLE IF NOT EXISTS audit_ledger (
    record_id    BIGSERIAL PRIMARY KEY,
    trace_id     TEXT,
    session_id   TEXT,
    member_ref   TEXT,
    event_type   TEXT NOT NULL,          -- turn | policy | tool | security | terminal
    actor        TEXT NOT NULL,          -- member | agent | system | human_agent
    action       TEXT NOT NULL,
    occurred_at  TEXT NOT NULL,          -- ISO-8601 UTC; the exact string that was hashed
    inputs       TEXT NOT NULL,          -- canonical JSON
    decision     TEXT,                   -- canonical JSON, policy events only
    prev_hash    CHAR(64) NOT NULL,
    record_hash  CHAR(64) NOT NULL,
    inputs_j     JSONB GENERATED ALWAYS AS (inputs::jsonb) STORED,
    decision_j   JSONB GENERATED ALWAYS AS (
                     CASE WHEN decision IS NULL THEN NULL ELSE decision::jsonb END
                 ) STORED
);
CREATE INDEX IF NOT EXISTS idx_ledger_session ON audit_ledger (session_id, record_id);
CREATE INDEX IF NOT EXISTS idx_ledger_member  ON audit_ledger (member_ref, occurred_at);
CREATE INDEX IF NOT EXISTS idx_ledger_event   ON audit_ledger (event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_ledger_dec     ON audit_ledger USING GIN (decision_j);

-- Enforcement, not decoration: this blocks UPDATE and DELETE for everyone,
-- including the table owner. A superuser can still drop the trigger — which is
-- exactly why the chain head is also anchored outside the database. Having to
-- disable this trigger to run the tamper demo IS the demonstration.
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

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    action      TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Chain-head anchor. One row per anchoring event, written at session close.
-- In production this is mirrored to S3 Object Lock; locally it is just proof
-- the head was observed at a point in time.
CREATE TABLE IF NOT EXISTS chain_anchors (
    id          BIGSERIAL PRIMARY KEY,
    record_id   BIGINT NOT NULL,
    chain_head  CHAR(64) NOT NULL,
    session_id  TEXT,
    anchored_at TEXT NOT NULL
);
"""


def _get_pool() -> ConnectionPool:
    """Lazily open the pool so importing this module never needs a live server."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=config.PG_DSN,
            min_size=config.PG_POOL_MIN,
            max_size=config.PG_POOL_MAX,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


def close_pool() -> None:
    """Shut the pool down. Call on application shutdown and between test runs."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def db_session() -> Iterator[Any]:
    """Read (or standalone-write) connection. Commits on success, rolls back on error.

    For anything that appends to the ledger, use `write_transaction()` instead —
    this one does not hold the chain lock.
    """
    with _get_pool().connection() as conn:
        yield conn


@contextmanager
def write_transaction() -> Iterator[Any]:
    """One serialised, all-or-nothing write transaction.

    Takes the chain advisory lock up front, so a mutation, its ledger row and
    its idempotency record commit together and no other writer can slip an
    append in between. Audit events queued during the body are shipped to Splunk
    only after the commit lands.
    """
    token = _pending_audit.set([])
    try:
        with _get_pool().connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (config.LEDGER_LOCK_KEY,))
            yield conn
        # Reaching here means the pool's context manager committed cleanly.
        events = _pending_audit.get() or []
        if events:
            splunk.ship_many(events)
    finally:
        _pending_audit.reset(token)


def queue_audit_event(event: dict[str, Any]) -> None:
    """Hold an audit event for post-commit shipping.

    Called by the ledger during a write transaction. Outside a transaction there
    is nothing to wait for, so the event ships immediately.
    """
    pending = _pending_audit.get()
    if pending is None:
        splunk.ship_many([event])
    else:
        pending.append(event)


def init_db() -> None:
    """Create tables, indexes and the immutability trigger. Safe to re-run."""
    with _get_pool().connection() as conn:
        conn.execute(_SCHEMA)


def tables_empty() -> bool:
    """True when there are no accounts yet — used to gate seeding on startup."""
    with db_session() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()
        return row["n"] == 0


def ping() -> bool:
    """True when Postgres answers. Used by the health endpoint and init script."""
    try:
        with db_session() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False
