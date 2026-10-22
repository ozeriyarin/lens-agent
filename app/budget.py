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
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

# USD per 1M tokens: (input, output). Checked 2026-10 against provider price pages.
# Gemini Flash has a free tier; these are the paid-tier numbers, i.e. worst case.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "google:gemini-2.5-flash": (0.30, 2.50),
    "anthropic:claude-haiku-4-5": (1.00, 5.00),
    "google:gemini-embedding-001": (0.15, 0.0),  # text embeddings (feature 004); free tier exists
}
# Unknown model string -> assume the most expensive tier we know. Refusing a
# call we could afford is a recoverable mistake; overspending is not.
_CONSERVATIVE_PRICE = max(PRICES_USD_PER_MTOK.values())

# Rough shape of one scan: a <=1024px image (~260 tokens on Gemini, ~1500 on
# Claude), a ~300 token system prompt, and a JSON ScanResult of a few hundred
# tokens. Deliberately rounded *up*.
SCAN_ESTIMATE_TOKENS = {"input_tokens": 2000, "output_tokens": 600}

# reserved: estimate held, call in flight. settled: real cost known.
# failed: the call errored after tokens were sent; the estimate stays charged
# because the provider still bills us and we have no exact figure.
Status = Literal["reserved", "settled", "failed"]

# primary: the cheap first call. escalation: the second-tier call feature 002
# makes when the primary result was weak. embedding: feature 004's memory call.
# Lets /stats count escalations and keep scan counts honest.
Tier = Literal["primary", "escalation", "embedding"]
_TIERS = ("primary", "escalation", "embedding")

# tap: the person pressed Scan. live: feature 003's loop sent the frame.
# Live frames are the volume driver, so stats and evals want to tell them apart.
Source = Literal["tap", "live"]

# Calibration (002 FR-4): once a model has a few settled calls, estimate from
# what it actually cost us rather than the fixed token table.
CALIBRATION_WINDOW = 20
CALIBRATION_MIN_ROWS = 3
CALIBRATION_SAFETY = 1.25


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
    tier: Tier = "primary"
    source: Source = "tap"


@dataclass(frozen=True)
class ModelStats:
    """One row of the per-model breakdown in /stats (002 FR-5)."""

    model: str
    calls: int
    failed: int
    cost_usd: float
    avg_latency_ms: int


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD for a call of this size on this model (conservative for unknown models)."""
    price_in, price_out = PRICES_USD_PER_MTOK.get(model, _CONSERVATIVE_PRICE)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def estimate_scan_cost(model: str) -> float:
    """What we assume one scan costs before we have real token counts."""
    return estimate_cost(model, **SCAN_ESTIMATE_TOKENS)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _month_start(now: datetime) -> str:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


_COLUMNS = "request_id, ts, model, input_tokens, output_tokens, cost_usd, latency_ms, status, tier, source"


def _row_to_entry(r: tuple) -> SpendEntry:
    return SpendEntry(r[0], datetime.fromisoformat(r[1]), r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9])


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
                    status        TEXT NOT NULL CHECK (status IN ('reserved', 'settled', 'failed')),
                    tier          TEXT NOT NULL DEFAULT 'primary' CHECK (tier IN ('primary', 'escalation', 'embedding')),
                    source        TEXT NOT NULL DEFAULT 'tap' CHECK (source IN ('tap', 'live'))
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS spend_ts ON spend(ts)")
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring ledgers from earlier features up to date, in place.

        001 -> tier column, 002 -> source column (ALTER ADD COLUMN). 004 -> the tier CHECK
        must accept 'embedding'; SQLite cannot alter a CHECK, so the table is rebuilt once.
        """
        cols = {row[1] for row in conn.execute("PRAGMA table_info(spend)")}
        if "tier" not in cols:
            conn.execute("ALTER TABLE spend ADD COLUMN tier TEXT NOT NULL DEFAULT 'primary'")
        if "source" not in cols:
            conn.execute("ALTER TABLE spend ADD COLUMN source TEXT NOT NULL DEFAULT 'tap'")
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'spend'").fetchone()[0]
        if "'embedding'" not in ddl:
            conn.execute("BEGIN")
            conn.execute(
                "CREATE TABLE spend_new (request_id TEXT PRIMARY KEY, ts TEXT NOT NULL, model TEXT NOT NULL, "
                "input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL, "
                "latency_ms INTEGER NOT NULL, status TEXT NOT NULL CHECK (status IN ('reserved', 'settled', 'failed')), "
                "tier TEXT NOT NULL DEFAULT 'primary' CHECK (tier IN ('primary', 'escalation', 'embedding')), "
                "source TEXT NOT NULL DEFAULT 'tap' CHECK (source IN ('tap', 'live')))"
            )
            conn.execute(f"INSERT INTO spend_new ({_COLUMNS}) SELECT {_COLUMNS} FROM spend")
            conn.execute("DROP TABLE spend")
            conn.execute("ALTER TABLE spend_new RENAME TO spend")
            conn.execute("CREATE INDEX IF NOT EXISTS spend_ts ON spend(ts)")
            conn.execute("COMMIT")

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

    def per_model(self, now: datetime | None = None) -> list[ModelStats]:
        """002 FR-5: this month's calls, failures, cost and mean latency per model."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT model,
                       COUNT(*)                                                    AS calls,
                       SUM(status = 'failed')                                      AS failed,
                       COALESCE(SUM(cost_usd), 0)                                  AS cost,
                       COALESCE(AVG(CASE WHEN status = 'settled' THEN latency_ms END), 0) AS lat
                FROM spend WHERE ts >= ? GROUP BY model ORDER BY cost DESC
                """,
                (_month_start(now or _utcnow()),),
            ).fetchall()
        return [ModelStats(r[0], r[1], r[2], float(r[3]), int(round(r[4]))) for r in rows]

    def scans_by_source(self, now: datetime | None = None) -> dict[str, int]:
        """003 FR-7: primary calls this month split into tap vs live."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT source, COUNT(*) FROM spend WHERE ts >= ? AND tier = 'primary' GROUP BY source",
                (_month_start(now or _utcnow()),),
            ).fetchall()
        counts = {"tap": 0, "live": 0}
        counts.update({r[0]: int(r[1]) for r in rows})
        return counts

    def escalations(self, now: datetime | None = None) -> int:
        """How many escalation-tier calls this month (002 FR-5)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM spend WHERE ts >= ? AND tier = 'escalation'",
                (_month_start(now or _utcnow()),),
            ).fetchone()
        return int(row[0])

    def estimate_for(self, model: str) -> float:
        """002 FR-4: what to reserve for the next call on this model.

        The *highest* cost among the last CALIBRATION_WINDOW settled calls, times
        a safety factor, once we have CALIBRATION_MIN_ROWS of them; the fixed
        token table before that. Max rather than mean: a reservation that is too
        small lets concurrent calls overshoot the cap, so the estimate tracks the
        worst recent case, not the typical one. Failed rows (estimates, not
        measurements) and zero-cost rows are excluded.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT cost_usd FROM spend WHERE model = ? AND status = 'settled' AND cost_usd > 0 "
                "ORDER BY ts DESC LIMIT ?",
                (model, CALIBRATION_WINDOW),
            ).fetchall()
        if len(rows) < CALIBRATION_MIN_ROWS:
            return estimate_scan_cost(model)
        return CALIBRATION_SAFETY * max(r[0] for r in rows)

    # --- the gate: reserve -> (settle | release) --------------------------------

    def reserve(
        self,
        model: str,
        estimated_cost_usd: float,
        *,
        request_id: str | None = None,
        now: datetime | None = None,
        tier: Tier = "primary",
        source: Source = "tap",
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
                    f"INSERT INTO spend ({_COLUMNS}) VALUES (?, ?, ?, 0, 0, ?, 0, 'reserved', ?, ?)",
                    (request_id, ts.isoformat(), model, estimated_cost_usd, tier, source),
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

    def fail(
        self,
        request_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
    ) -> SpendEntry | None:
        """The call errored *after* reaching the provider: charge the larger of the
        reserved estimate and the usage we did observe.

        Overcounting a failed call is the safe direction (constitution II);
        the next month resets it anyway. Returns the stored row, or None when
        there was no live reservation to fail.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT model, cost_usd FROM spend WHERE request_id = ? AND status = 'reserved'", (request_id,)
            ).fetchone()
            if row is None:
                return None  # already settled/failed, or never reserved: nothing to do
            model, reserved = row
            cost = max(float(reserved), estimate_cost(model, input_tokens, output_tokens))
            conn.execute(
                "UPDATE spend SET status = 'failed', input_tokens = ?, output_tokens = ?, cost_usd = ?, "
                "latency_ms = ? WHERE request_id = ?",
                (input_tokens, output_tokens, cost, latency_ms, request_id),
            )
            stored = conn.execute(f"SELECT {_COLUMNS} FROM spend WHERE request_id = ?", (request_id,)).fetchone()
        return _row_to_entry(stored)

    def release(self, request_id: str) -> None:
        """Nothing was sent to the provider (we never got that far): give the money back.

        Only for failures *before* the request leaves the process. If in doubt, use `fail`.
        """
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
