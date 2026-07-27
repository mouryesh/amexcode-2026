"""shared/observability.py — structured, one-line-per-request operational logging.

NOT the audit ledger (backend/ledger.py is the tamper-proof decision trail).
This is operational visibility: request latency, outcomes, error rates — what
you'd actually watch on a dashboard to know if the system is degrading, as
distinct from what you'd replay to prove a specific decision was correct.

One structured JSON line per event, so it's greppable and pipeable to any log
aggregator without a schema migration.

Imports: stdlib only.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_logger = logging.getLogger("servicing_agent")
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False  # don't also go through the root logger's handlers


def log_event(event: str, **fields: Any) -> None:
    """Emit one structured JSON log line: {"event": ..., "timestamp": ..., **fields}."""
    record = {"event": event, "timestamp": datetime.now(timezone.utc).isoformat(), **fields}
    _logger.info(json.dumps(record, sort_keys=True, default=str))
