"""Optional tracing with Logfire (feature 007 FR-5).

Why optional and why Logfire: Pydantic AI instruments itself with one call and Logfire
shows each model call as a span with token usage, which is exactly the "where did the
money and time go" view. It needs an account token and a network connection, so it is
behind `LOGFIRE_TOKEN`; without it this module is a no-op and the app has no extra
dependency at runtime. The JSON scan events (events.py) are always on and are enough
for grep-level observability.

    uv add logfire            # once
    LOGFIRE_TOKEN=...         # in .env; then start the app
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def configure(app=None) -> bool:
    """Instrument Pydantic AI (and the FastAPI app, if given) when LOGFIRE_TOKEN is set and
    logfire is installed. Returns True when tracing is on."""
    token = os.getenv("LOGFIRE_TOKEN", "").strip()
    if not token:
        return False
    try:
        import logfire  # optional dependency
    except ImportError:
        log.warning("LOGFIRE_TOKEN is set but logfire is not installed: run `uv add logfire`")
        return False
    try:
        logfire.configure(token=token, service_name="lens-agent", send_to_logfire=True)
        logfire.instrument_pydantic_ai()
        if app is not None:
            logfire.instrument_fastapi(app)
        log.info("tracing on: Logfire")
        return True
    except Exception as e:  # tracing must never take the app down
        log.warning("tracing disabled: %s: %s", type(e).__name__, str(e)[:200])
        return False
