"""Scan events: one JSON line per /scan (feature 007 FR-4).

Why: the ledger holds the money per model call; the event line adds what the ledger
cannot see, per request: where the time went (gate, model, memory), whether the cache
answered, how many entities and passages were involved, and the HTTP status, all keyed
by the same request id that the ledger row and the `X-Request-Id` header carry. JSON
lines are grep-able, ship to any log system, and cost nothing.

    docker compose logs -f lens | grep '"event": "scan"'
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field

log = logging.getLogger("lens.events")


@dataclass
class ScanEvent:
    request_id: str
    source: str  # tap | live
    status: int = 200
    cached: bool = False
    question: bool = False
    model: str = ""
    primary_model: str = ""
    escalated: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0  # whole request
    timings_ms: dict[str, int] = field(default_factory=dict)  # gate, model, memory
    entities: int = 0  # linked entities (known or new)
    known: int = 0  # of which seen before
    sources: int = 0  # passages cited
    private: bool = True
    error: str | None = None

    def emit(self) -> None:
        log.info(json.dumps({"event": "scan", **asdict(self)}, ensure_ascii=False))


def configure_logging() -> None:
    """Make sure app log lines reach stdout under uvicorn (which only configures its own
    loggers). Idempotent; honours a handler the host already installed."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("lens.events").setLevel(logging.INFO)
    logging.getLogger("app").setLevel(logging.INFO)
