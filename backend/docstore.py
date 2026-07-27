"""backend/docstore.py — session documents. MongoDB when available, SQL otherwise.

Flow state is a nested document, so it wants a document store. But the system
has to run on a laptop with nothing installed, so when pymongo or the server is
absent the same documents live in a single JSON column on the SQL engine.

Whole-document get/put rather than Mongo update operators. That keeps one
implementation of the mutation logic in `sessions.py` instead of `$set`/`$push`
on one path and read-modify-write on the other — and update operators would
have to be reimplemented for SQL anyway.

# ponytail: read-modify-write, so two concurrent turns on ONE session could lose
# an update. A session is one person typing, so turns are serial in practice.
# If that stops being true, add a version column and compare-and-swap on put().

Imports: backend.database, shared.config.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from backend.database import db_session
from shared.config import config

_client: Any = None
_BACKEND: Optional[str] = None

_SQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_docs (
    session_id TEXT PRIMARY KEY,
    doc        TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _detect() -> str:
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    if config.MONGO_URI:
        try:
            from pymongo import MongoClient

            c = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=1500, tz_aware=False)
            c.admin.command("ping")
            globals()["_client"] = c
            _BACKEND = "mongo"
            return _BACKEND
        except Exception:
            pass  # not installed, or not running — fall through to SQL
    _BACKEND = "sql"
    return _BACKEND


def backend() -> str:
    return _detect()


def _col():
    return _client[config.MONGO_DB]["sessions"]


def init() -> None:
    """Create indexes (Mongo) or the documents table (SQL). Safe to re-run."""
    if _detect() == "mongo":
        col = _col()
        col.create_index("member_ref")
        col.create_index("session_status")
        col.create_index("last_activity_at")
    else:
        with db_session() as conn:
            conn.execute(_SQL_SCHEMA)


def get(session_id: str) -> Optional[dict[str, Any]]:
    if _detect() == "mongo":
        doc = _col().find_one({"_id": session_id})
        if doc:
            doc.pop("_id", None)
        return doc
    with db_session() as conn:
        row = conn.execute(
            "SELECT doc FROM session_docs WHERE session_id = %s", (session_id,)
        ).fetchone()
        return json.loads(row["doc"]) if row else None


def put(session_id: str, doc: dict[str, Any]) -> None:
    if _detect() == "mongo":
        _col().replace_one({"_id": session_id}, {**doc, "_id": session_id}, upsert=True)
        return
    payload = json.dumps(doc, default=str)
    updated = doc.get("last_activity_at", "")
    with db_session() as conn:
        # UPSERT syntax is identical on SQLite 3.24+ and Postgres 9.5+.
        conn.execute(
            """INSERT INTO session_docs (session_id, doc, updated_at)
               VALUES (%s, %s, %s)
               ON CONFLICT (session_id) DO UPDATE SET doc = %s, updated_at = %s""",
            (session_id, payload, updated, payload, updated),
        )


def delete(session_id: str) -> None:
    if _detect() == "mongo":
        _col().delete_one({"_id": session_id})
        return
    with db_session() as conn:
        conn.execute("DELETE FROM session_docs WHERE session_id = %s", (session_id,))


def all_sessions() -> list[dict[str, Any]]:
    if _detect() == "mongo":
        return [{**d, "session_id": d.pop("_id")} for d in _col().find()]
    with db_session() as conn:
        rows = conn.execute("SELECT doc FROM session_docs ORDER BY updated_at DESC").fetchall()
        return [json.loads(r["doc"]) for r in rows]


def ping() -> bool:
    try:
        if _detect() == "mongo":
            _client.admin.command("ping")
        else:
            with db_session() as conn:
                conn.execute("SELECT 1 FROM session_docs LIMIT 1")
        return True
    except Exception:
        return False


def stats() -> dict[str, Any]:
    try:
        docs = all_sessions()
        return {
            "backend": _detect(),
            "sessions": len(docs),
            "open": sum(1 for d in docs if d.get("session_status") == "open"),
        }
    except Exception:
        return {"backend": _detect(), "sessions": None, "open": None}
