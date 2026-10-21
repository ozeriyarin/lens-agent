"""Runtime settings, read once from the environment / `.env` (CLAUDE.md rule 5).

Why a module and not `os.environ` everywhere: one place lists every knob the
app has, with its default and its type, and tests can build a `Settings`
directly without touching the real environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MODEL = "google:gemini-2.5-flash"
DEFAULT_ESCALATION_MODEL = "anthropic:claude-haiku-4-5"
DEFAULT_ESCALATE_BELOW = 0.6


@dataclass(frozen=True)
class Settings:
    model: str = DEFAULT_MODEL
    budget_usd: float = 5.0
    db_path: Path = Path("lens.db")
    cache_size: int = 32
    # Shared secret for /scan and /stats. Empty = open (fine on localhost). Set it
    # whenever the server is reachable from the internet (cloudflared/ngrok), or
    # anyone with the URL can spend the month's budget.
    scan_token: str = ""
    # Feature 002: second tier. Empty string disables escalation (001 behaviour).
    escalation_model: str = DEFAULT_ESCALATION_MODEL
    # A primary result whose best object confidence is below this is "weak".
    escalate_below: float = DEFAULT_ESCALATE_BELOW

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = None) -> "Settings":
        load_dotenv(dotenv_path)  # no-op when there is no .env; never overrides real env vars
        return cls(
            model=os.getenv("MODEL", DEFAULT_MODEL),
            budget_usd=float(os.getenv("BUDGET_USD", "5")),
            db_path=Path(os.getenv("DB_PATH", "lens.db")),
            cache_size=int(os.getenv("CACHE_SIZE", "32")),
            scan_token=os.getenv("SCAN_TOKEN", "").strip(),
            escalation_model=os.getenv("ESCALATION_MODEL", DEFAULT_ESCALATION_MODEL).strip(),
            escalate_below=float(os.getenv("ESCALATE_BELOW", str(DEFAULT_ESCALATE_BELOW))),
        )
