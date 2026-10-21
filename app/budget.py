"""Budget guard: the hard monthly spend cap (constitution II).

Why this file exists: the provider dashboards are not something our code can
query cheaply or test offline, so we keep our own ledger. Every paid call is
written to a SQLite table *after* it happens, and *before* any paid call we
add an estimate for that call to the month-to-date total and compare it with
the cap. If the estimate would push us over, the call is refused with
`BudgetExceeded`: loud, never a silent downgrade.

Design choices:
- SQLite over an in-memory counter: the cap must survive restarts, and the
  ledger doubles as the data behind `GET /stats` (feature 002).
- Estimate before, record after: we cannot know the real token count until
  the model answers, so the pre-check uses a conservative estimate and the
  ledger stores the real numbers. The guard errs on the side of refusing.
- Prices live in one dict with a conservative fallback for unknown models.
  They are *estimates* (provider prices change); the provider-side spend
  limit is the second line of defence.
- One short-lived connection per operation: simple and safe if FastAPI runs
  handlers in a thread pool. The ledger sees a handful of writes per day,
  so there is nothing here worth optimising.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# USD per 1M tokens: (input, output). Checked 2026-10 against provider price pages.
# Gemini Flash has a free tier; these are the paid-tier numbers, i.e. worst case.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "google-gla:gemini-2.5-flash": (0.30, 2.50),
    "anthropic:claude-haiku-4-5": (1.00, 5.00),
}
# Unknown model string -> assume the most expensive tier we know. Refusing a
# call we could afford is a recoverable mistake; overspending is not.
_CONSERVATIVE_PRICE = max(PRICES_USD_PER_MTOK.values())

# Rough shape of one scan: a <=1024px image (~260 tokens on Gemini, ~1500 on
# Claude), a ~300 token system prompt, and a JSON ScanResult of a few hundred
# tokens. Deliberately rounded *up*.
SCAN_ESTIMATE_TOKENS = {"input_tokens": 2000, "output_tokens": 600}


class BudgetExceeded(RuntimeError):
    """Raised when a call would push month-to-date spend over the cap."""


@dataclass(frozen=True)
class SpendEntry:
    """One row of the ledger: what we paid, for what, and how long it took."""

    request_id: str
    ts: datetime
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD for a call of this size on this model (conservative for unknown models)."""
    price_in, price_out = PRICES_USD_PER_MTOK.get(model, _CONSERVATIVE_PRICE)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def estimate_scan_cost(model: str) -> float:
    """What we assume one scan costs before we have real token counts."""
    return estimate_cost(model, **SCAN_ESTIMATE_TOKENS)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class Budget:
    """Monthly spend ledger plus the cap check. One instance per app."""

    def __init__(self, db_path: str | Path, cap_usd: float) -> None:
        if cap_usd <= 0:
            raise ValueError("cap_usd must be positive")
        self.db_path = str(db_path)
        self.cap_usd = cap_usd
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS spend (
                    request_id    TEXT PRIMARY KEY,
                    ts            TEXT NOT NULL,
                    model         TEXT NOT NULL,
                    input_tokens  INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd      REAL NOT NULL,
                    latency_ms    INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS spend_ts ON spend(ts)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    # --- reading -----------------------------------------------------------

    def month_to_date(self, now: datetime | None = None) -> float:
        """USD spent since the first of the current (UTC) month."""
        start = _month_start(now or _utcnow()).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE ts >= ?", (start,)
            ).fetchone()
        return float(row[0])

    def remaining(self, now: datetime | None = None) -> float:
        return max(0.0, self.cap_usd - self.month_to_date(now))

    def entries(self, now: datetime | None = None) -> list[SpendEntry]:
        """This month's ledger rows, oldest first (for /stats and tests)."""
        start = _month_start(now or _utcnow()).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT request_id, ts, model, input_tokens, output_tokens, cost_usd, latency_ms "
                "FROM spend WHERE ts >= ? ORDER BY ts",
                (start,),
            ).fetchall()
        return [
            SpendEntry(r[0], datetime.fromisoformat(r[1]), r[2], r[3], r[4], r[5], r[6])
            for r in rows
        ]

    # --- the gate ------------------------------------------------------------

    def can_spend(self, estimated_cost_usd: float, now: datetime | None = None) -> bool:
        """FR-5: month-to-date + estimate must stay <= cap."""
        return self.month_to_date(now) + estimated_cost_usd <= self.cap_usd

    def check(self, estimated_cost_usd: float, now: datetime | None = None) -> None:
        """Same test as `can_spend`, but refuses loudly (constitution II)."""
        spent = self.month_to_date(now)
        if spent + estimated_cost_usd > self.cap_usd:
            raise BudgetExceeded(
                f"monthly budget reached: spent {spent:.4f} USD, this call ~{estimated_cost_usd:.4f} USD, "
                f"cap {self.cap_usd:.2f} USD"
            )

    # --- writing -------------------------------------------------------------

    def record(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        request_id: str | None = None,
        cost_usd: float | None = None,
        now: datetime | None = None,
    ) -> SpendEntry:
        """FR-6: log one paid call with its real token counts.

        `cost_usd` is computed from the price table unless the caller already
        knows the exact figure. Returns the stored row so callers can surface
        it (the PWA shows cost per scan).
        """
        entry = SpendEntry(
            request_id=request_id or uuid.uuid4().hex,
            ts=now or _utcnow(),
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd if cost_usd is not None else estimate_cost(model, input_tokens, output_tokens),
            latency_ms=latency_ms,
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO spend VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.request_id,
                    entry.ts.isoformat(),
                    entry.model,
                    entry.input_tokens,
                    entry.output_tokens,
                    entry.cost_usd,
                    entry.latency_ms,
                ),
            )
        return entry
