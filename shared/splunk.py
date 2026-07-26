"""shared/splunk.py — the Splunk HEC client. The audit mirror, never the source of truth.

Postgres holds the hash chain; Splunk holds a searchable copy of the same events
so the audit viewer, the repeat-decline query and the quality-gate dashboards
have something to search. That ordering is deliberate and load-bearing:

  * Events are shipped **after** the database transaction commits, so Splunk can
    never show an event Postgres does not have.
  * Shipping is **asynchronous** on a background worker, so a slow or dead
    Splunk adds no latency to a member's request.
  * Shipping is **best-effort**. If Splunk is unreachable the events are counted
    as dropped and the agent carries on. You lose search, not the record — the
    chain in Postgres is still complete and still verifies.

Getting this backwards — writing to Splunk first, or treating it as the record —
would collapse the audit claim, because you could no longer prove the log
matches what the database actually did.

Imports: shared.config only.
"""
from __future__ import annotations

import atexit
import json
import queue
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from shared.config import config

_QUEUE: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue(maxsize=config.SPLUNK_QUEUE_MAX)
_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()
_session: Any = None

# Observability for the observability layer: if these climb, the mirror is
# lagging or broken. The chain is unaffected either way.
_stats = {"shipped": 0, "dropped_full": 0, "dropped_error": 0}
_stats_lock = threading.Lock()


def _bump(key: str) -> None:
    with _stats_lock:
        _stats[key] += 1


def stats() -> dict[str, int]:
    """Counters for the health endpoint."""
    with _stats_lock:
        return dict(_stats)


def _http():
    """Lazily build a requests Session so importing never needs the dependency."""
    global _session
    if _session is None:
        import requests

        s = requests.Session()
        s.headers.update({"Authorization": f"Splunk {config.SPLUNK_HEC_TOKEN}"})
        if not config.SPLUNK_VERIFY_TLS:
            # Local Splunk ships a self-signed cert. Never do this against a
            # real deployment — point SPLUNK_VERIFY_TLS at a CA bundle instead.
            s.verify = False
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        _session = s
    return _session


def _envelope(event: dict[str, Any]) -> dict[str, Any]:
    """Wrap one audit event in the HEC envelope."""
    return {
        "time": datetime.now(timezone.utc).timestamp(),
        "host": "servicing-agent",
        "source": "backend.ledger",
        "sourcetype": config.SPLUNK_SOURCETYPE,
        "index": config.SPLUNK_INDEX,
        "event": event,
    }


def _post(batch: list[dict[str, Any]]) -> None:
    """HEC accepts newline-delimited JSON objects in one request."""
    body = "\n".join(json.dumps(_envelope(e), sort_keys=True, default=str) for e in batch)
    resp = _http().post(
        config.SPLUNK_HEC_URL, data=body.encode("utf-8"), timeout=config.SPLUNK_TIMEOUT_S
    )
    resp.raise_for_status()


def _drain() -> None:
    """Background worker: batch whatever is queued and post it."""
    while True:
        item = _QUEUE.get()
        if item is None:  # shutdown sentinel
            _QUEUE.task_done()
            return
        batch = [item]
        # Opportunistically coalesce anything already waiting.
        while len(batch) < 100:
            try:
                nxt = _QUEUE.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                _QUEUE.task_done()
                _flush_batch(batch)
                return
            batch.append(nxt)
        _flush_batch(batch)
        for _ in batch:
            _QUEUE.task_done()


def _flush_batch(batch: list[dict[str, Any]]) -> None:
    try:
        _post(batch)
        with _stats_lock:
            _stats["shipped"] += len(batch)
    except Exception:
        # Deliberately swallowed. A broken mirror must never break servicing,
        # and must never raise into the request path.
        with _stats_lock:
            _stats["dropped_error"] += len(batch)


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_drain, name="splunk-hec", daemon=True)
            _worker.start()


def ship_many(events: list[dict[str, Any]]) -> None:
    """Queue audit events for shipping. Returns immediately; never raises."""
    if not config.splunk_configured() or not events:
        return
    _ensure_worker()
    for e in events:
        try:
            _QUEUE.put_nowait(e)
        except queue.Full:
            _bump("dropped_full")


def flush(timeout: float = 5.0) -> bool:
    """Block until the queue drains. For tests and clean shutdown only.

    Returns False on timeout — meaning some events are still in flight, not that
    they were lost.
    """
    if not config.splunk_configured():
        return True
    done = threading.Event()

    def _waiter() -> None:
        _QUEUE.join()
        done.set()

    threading.Thread(target=_waiter, daemon=True).start()
    return done.wait(timeout)


def ping() -> bool:
    """True when HEC accepts a health event. Used by the init script."""
    if not config.splunk_configured():
        return False
    try:
        _post([{"event_type": "health", "action": "ping"}])
        return True
    except Exception:
        return False


@atexit.register
def _shutdown() -> None:
    if _worker is not None and _worker.is_alive():
        try:
            _QUEUE.put_nowait(None)
        except queue.Full:
            pass
        _worker.join(timeout=3)
