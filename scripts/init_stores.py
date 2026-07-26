"""Wait for the three stores, then create schema and indexes.

    docker compose up -d
    python scripts/init_stores.py

Splunk takes ~60s on first boot. Postgres and Mongo are hard requirements;
Splunk is not — if it never answers, the agent still runs and the chain is
still authoritative, you just lose search and dashboards.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import database, mongo  # noqa: E402
from shared import splunk  # noqa: E402
from shared.config import config  # noqa: E402


def wait(name: str, check, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            print(f"  {name}: up")
            return True
        time.sleep(2)
    print(f"  {name}: TIMEOUT after {timeout}s")
    return False


def main() -> int:
    print("Waiting for stores...")
    pg_ok = wait("postgres", database.ping, 60)
    mongo_ok = wait("mongo", mongo.ping, 30)

    if not (pg_ok and mongo_ok):
        print("\nPostgres and Mongo are required. Is `docker compose up -d` running?")
        return 1

    print("\nCreating schema...")
    database.init_db()
    print("  postgres: tables, indexes, immutability trigger")
    mongo.init_indexes()
    print("  mongo: session indexes + TTL")

    if config.splunk_configured():
        if wait("splunk hec", splunk.ping, 180):
            splunk.flush()
            print("  splunk: index reachable")
        else:
            print("  splunk: unreachable — continuing without the audit mirror")
    else:
        print("  splunk: disabled (SPLUNK_ENABLED=false)")

    print("\nReady. Next: python backend/seed.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
