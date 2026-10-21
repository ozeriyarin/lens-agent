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

DEFAULT_MODEL = "google-gla:gemini-2.5-flash"


@dataclass(frozen=True)
class Settings:
    model: str = DEFAULT_MODEL
    budget_usd: float = 5.0
    db_path: Path = Path("lens.db")
    cache_size: int = 32

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = None) -> "Settings":
        load_dotenv(dotenv_path)  # no-op when there is no .env; never overrides real env vars
        return cls(
            model=os.getenv("MODEL", DEFAULT_MODEL),
            budget_usd=float(os.getenv("BUDGET_USD", "5")),
            db_path=Path(os.getenv("DB_PATH", "lens.db")),
            cache_size=int(os.getenv("CACHE_SIZE", "32")),
        )
