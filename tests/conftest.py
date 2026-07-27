"""Pytest fixtures — isolate every test run in a throwaway SQLite database.

Sets DB_PATH before any project module imports shared.config, so the whole
suite (ledger, classifier, policy) never touches the demo database.
"""
import os
import tempfile

# Point the app at a temp DB for the whole test session. Must run at import
# time, before shared.config.Config reads the environment.
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DB_PATH"] = _TMP.name
os.environ.setdefault("LLM_PROVIDER", "offline")

# Pin the engine, don't just point at a temp file. A developer with PG_DSN set
# in .env would otherwise run the whole suite against their real Postgres: the
# tests write raw SQLite SQL (`?` placeholders, sqlite_sequence), so they failed
# loudly rather than corrupting anything — but `pytest -q` was red out of the
# box for anyone following RUN.md's Postgres setup. The suite chooses SQLite;
# scripts/smoke.py is what exercises Postgres.
os.environ["PG_DSN"] = ""
os.environ["MONGO_URI"] = ""
