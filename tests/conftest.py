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
