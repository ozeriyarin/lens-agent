"""Budget guard: the hard monthly spend cap (constitution II).

Why this file exists: the provider dashboards are not something our code can
query cheaply or test offline, so we keep our own ledger. Before any paid
call we *reserve* an estimate in a SQLite table; after the call we *settle*
the reservation with the real token counts (or *release* it if the call
failed). If the reservation would push month-to-date spend over the cap, it
is refused with `BudgetExceeded`: loud, never a silent downgrade.

Design choices:
- Reserve/settle instead of check-then-record: a plain "read the sum, compare,
  then insert later" has a race. Two scans arriving together both read the
  same sum, both pass, both pay. Doing the sum and the insert inside one
  `BEGIN IMMEDIATE` transaction makes the gate atomic: SQLite takes the write
  lock first, so the second caller waits and then sees the first reservation.
- SQLite over an in-memory counter: the cap must survive restarts, and the
  ledger doubles as the data behind `GET /stats`.
- Estimate before, real numbers after: we cannot know the token count until
  the model answers, so the reservation uses a conservative estimate. The
  guard errs on the side of refusing.
- Prices live in one dict with a conservative fallback for unknown models.
  They are *estimates* (provider prices change); the provider-side spend
  limit is the second line of defence.
- One short-lived connection per operation: simple and safe when FastAPI runs
  handlers in a thread pool. A handful of writes per day, nothing to optimise.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

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

Status = Literal["reserved", "settled"]


class BudgetExceeded(RuntimeError):
    """Raised when a call would push month-to-date spend over the cap."""


@dataclass(frozen=True)
class SpendEntry:
    """One row of the ledger: what we paid (or reserved), for what, how long it took."""

    request_id: str
    ts: datetime
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    status: Status


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD for a call of this size on this model (conservative for unknown models)."""
    price_in, price_out = PRICES_USD_PER_MTOK.get(model, _CONSERVATIVE_PRICE)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def estimate_scan_cost(model: str) -> float:
    """What we assume one scan costs before we have real token counts."""
    return estimate_cost(model, **SCAN_ESTIMATE_TOKENS)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _month_start(now: datetime) -> str:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


_COLUMNS = "request_id, ts, model, input_tokens, output_tokens, cost_usd, latency_ms, status"


def _row_to_entry(r: tuple) -> SpendEntry:
    return SpendEntry(r[0], datetime.fromisoformat(r[1]), r[2], r[3], r[4], r[5], r[6], r[7])


class Budget:
    """Monthly spend ledger plus the atomic cap gate. One instance per app."""

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
                    latency_ms    INTEGER NOT NULL,
                    status        TEXT NOT NULL CHECK (status IN ('reserved', 'settled'))
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS spend_ts ON spend(ts)")

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: we issue BEGIN/COMMIT ourselves where it matters.
        return sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)

    # --- reading -------------------------------------------------------------

    def month_to_date(self, now: datetime | None = None) -> float:
        """USD spent *or reserved* since the first of the current (UTC) month."""
        with self._connect() as conn:
            return self._sum_since(conn, _month_start(now or _utcnow()))

    @staticmethod
    def _sum_since(conn: sqlite3.Connection, start_iso: str) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE ts >= ?", (start_iso,)
        ).fetchone()
        return float(row[0])

    def remaining(self, now: datetime | None = None) -> float:
        return max(0.0, self.cap_usd - self.month_to_date(now))

    def can_spend(self, estimated_cost_usd: float, now: datetime | None = None) -> bool:
        """Read-only preview of the gate (for /stats and the UI). Not a guarantee:
        the authoritative, race-free check is `reserve`."""
        return self.month_to_date(now) + estimated_cost_usd <= self.cap_usd

    def entries(self, now: datetime | None = None) -> list[SpendEntry]:
        """This month's ledger rows, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM spend WHERE ts >= ? ORDER BY ts",
                (_month_start(now or _utcnow()),),
            ).fetchall()
        return [_row_to_entry(r) for r in rows]

    # --- the gate: reserve -> (settle | release) --------------------------------

    def reserve(
        self,
        model: str,
        estimated_cost_usd: float,
        *,
        request_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """FR-5, atomically: refuse if month-to-date + estimate > cap, else hold the estimate.

        Returns the request id to pass to `settle` or `release`.
        """
        request_id = request_id or uuid.uuid4().hex
        ts = now or _utcnow()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")  # take the write lock before reading the sum
            try:
                spent = self._sum_since(conn, _month_start(ts))
                if spent + estimated_cost_usd > self.cap_usd:
                    raise BudgetExceeded(
                        f"monthly budget reached: spent {spent:.4f} USD, this call ~{estimated_cost_usd:.4f} USD, "
                        f"cap {self.cap_usd:.2f} USD"
                    )
                conn.execute(
                    f"INSERT INTO spend ({_COLUMNS}) VALUES (?, ?, ?, 0, 0, ?, 0, 'reserved')",
                    (request_id, ts.isoformat(), model, estimated_cost_usd),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return request_id

    def settle(
        self,
        request_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        cost_usd: float | None = None,
    ) -> SpendEntry:
        """FR-6: replace the reservation with what the call really cost."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT model, status FROM spend WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no reservation {request_id!r}")
            model, status = row
            if status != "reserved":
                raise ValueError(f"request {request_id!r} is already {status}")
            real_cost = cost_usd if cost_usd is not None else estimate_cost(model, input_tokens, output_tokens)
            conn.execute(
                "UPDATE spend SET input_tokens = ?, output_tokens = ?, cost_usd = ?, latency_ms = ?, "
                "status = 'settled' WHERE request_id = ?",
                (input_tokens, output_tokens, real_cost, latency_ms, request_id),
            )
            stored = conn.execute(
                f"SELECT {_COLUMNS} FROM spend WHERE request_id = ?", (request_id,)
            ).fetchone()
        return _row_to_entry(stored)

    def release(self, request_id: str) -> None:
        """The call failed before anything was billed: give the reservation back."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM spend WHERE request_id = ? AND status = 'reserved'", (request_id,)
            )

    # --- convenience ------------------------------------------------------------

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
        """Reserve and settle in one go, for calls whose cost is already known
        (backfills, tests). Live scans use reserve/settle so the gate is atomic."""
        cost = cost_usd if cost_usd is not None else estimate_cost(model, input_tokens, output_tokens)
        rid = self.reserve(model, cost, request_id=request_id, now=now)
        return self.settle(
            rid, input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=latency_ms, cost_usd=cost
        )
