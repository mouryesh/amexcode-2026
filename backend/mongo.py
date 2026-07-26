"""backend/mongo.py — MongoDB client for live conversation state.

Why Mongo and not another Postgres table: the flow state defined by the state
schema spec is a deeply nested object — session, member, active_flow, a map of
slot objects each with its own status and provenance, risk signals, dialogue
counters — and the whole thing is rewritten every turn. That is a document, not
a row. Normalising it would mean six tables and a join on every message for data
that is never queried relationally and never outlives the session.

The split to hold in mind:

    Mongo     mutable, current, short-lived   — what the agent is doing now
    Postgres  immutable, historical, chained  — what the agent decided and did

Nothing in Mongo is authoritative. If the flow state is lost, the session is
lost, but no decision or write is lost — those live in the ledger.

Imports: shared.config only.
"""
from __future__ import annotations

from typing import Any, Optional

from shared.config import config

_client: Any = None


def client():
    """Lazily connect so importing this module never needs a live server."""
    global _client
    if _client is None:
        from pymongo import MongoClient

        _client = MongoClient(
            config.MONGO_URI,
            serverSelectionTimeoutMS=3000,
            tz_aware=True,
        )
    return _client


def db():
    return client()[config.MONGO_DB]


def sessions_col():
    """One document per conversation, keyed by session_id."""
    return db()["sessions"]


def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


def init_indexes() -> None:
    """Create indexes. Safe to re-run."""
    col = sessions_col()
    col.create_index("member_ref")
    col.create_index("session_status")
    col.create_index("last_activity_at")
    # Flow state is disposable once the session is long finished. A TTL index
    # keeps Mongo from growing without bound; the audit trail is unaffected
    # because it lives in Postgres.
    col.create_index("expires_at", expireAfterSeconds=0)


def ping() -> bool:
    """True when Mongo answers. Used by the health endpoint and init script."""
    try:
        client().admin.command("ping")
        return True
    except Exception:
        return False


def stats() -> dict[str, Optional[int]]:
    try:
        return {
            "sessions": sessions_col().count_documents({}),
            "open": sessions_col().count_documents({"session_status": "open"}),
        }
    except Exception:
        return {"sessions": None, "open": None}
