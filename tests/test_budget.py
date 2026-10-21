"""Budget guard tests (constitution II and V).

The guard is the one piece of code that stands between us and a surprise
bill, so the tests pin down the boundary behaviour: exactly-at-cap is allowed,
one cent over is refused, last month's spend does not count, the ledger
survives a restart, and concurrent reservations cannot both squeeze through.
"""

import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from app.budget import (
    PRICES_USD_PER_MTOK,
    Budget,
    BudgetExceeded,
    estimate_cost,
    estimate_scan_cost,
)

GEMINI = "google:gemini-2.5-flash"
HAIKU = "anthropic:claude-haiku-4-5"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def budget(tmp_path):
    return Budget(tmp_path / "ledger.db", cap_usd=5.0)


def spend(budget, cost, now=NOW, **kw):
    return budget.record(model=GEMINI, input_tokens=0, output_tokens=0, latency_ms=0, cost_usd=cost, now=now, **kw)


def test_fresh_ledger_has_no_spend(budget):
    assert budget.month_to_date(NOW) == 0.0
    assert budget.remaining(NOW) == 5.0
    assert budget.can_spend(0.01, NOW)


def test_record_adds_to_month_to_date(budget):
    spend(budget, 1.25)
    spend(budget, 0.75)
    assert budget.month_to_date(NOW) == pytest.approx(2.0)
    assert budget.remaining(NOW) == pytest.approx(3.0)


def test_exactly_at_cap_is_allowed_one_cent_over_is_not(budget):
    spend(budget, 4.0)
    assert budget.can_spend(1.0, NOW)  # 4 + 1 == 5, FR-5 says <= cap
    assert not budget.can_spend(1.01, NOW)
    budget.reserve(GEMINI, 1.0, now=NOW)  # takes us exactly to the cap
    with pytest.raises(BudgetExceeded):
        budget.reserve(GEMINI, 0.01, now=NOW)


def test_refusal_is_loud_and_leaves_no_row(budget):
    spend(budget, 4.99)
    with pytest.raises(BudgetExceeded) as err:
        budget.reserve(HAIKU, 0.02, now=NOW)
    assert "cap 5.00" in str(err.value) and "4.9900" in str(err.value)
    assert len(budget.entries(NOW)) == 1  # the refused call left nothing behind


def test_reservation_counts_until_settled_or_released(budget):
    rid = budget.reserve(GEMINI, 3.0, now=NOW)
    assert budget.month_to_date(NOW) == pytest.approx(3.0)
    assert not budget.can_spend(2.5, NOW)  # the reservation is holding the money
    entry = budget.settle(rid, input_tokens=1500, output_tokens=400, latency_ms=900)
    assert entry.status == "settled"
    assert entry.cost_usd == pytest.approx(estimate_cost(GEMINI, 1500, 400))
    assert budget.month_to_date(NOW) == pytest.approx(entry.cost_usd)  # estimate replaced by real cost

    rid2 = budget.reserve(GEMINI, 1.0, now=NOW)
    budget.release(rid2)  # never reached the provider: nothing billed
    assert budget.month_to_date(NOW) == pytest.approx(entry.cost_usd)

    rid3 = budget.reserve(GEMINI, 1.0, now=NOW)
    budget.fail(rid3, latency_ms=50)  # reached the provider, then broke: estimate stays charged
    assert budget.month_to_date(NOW) == pytest.approx(entry.cost_usd + 1.0)
    assert [e.status for e in budget.entries(NOW)] == ["settled", "failed"]
    budget.fail(rid3)  # idempotent: only a 'reserved' row changes
    budget.release(rid3)  # and a failed row cannot be released
    assert budget.month_to_date(NOW) == pytest.approx(entry.cost_usd + 1.0)


def test_fail_charges_observed_usage_when_it_exceeds_the_estimate(budget):
    rid = budget.reserve(GEMINI, 0.0001, now=NOW)  # a deliberately tiny estimate
    budget.fail(rid, input_tokens=100_000, output_tokens=10_000, latency_ms=5)
    [entry] = budget.entries(NOW)
    assert entry.status == "failed"
    assert entry.cost_usd == pytest.approx(estimate_cost(GEMINI, 100_000, 10_000))
    assert entry.cost_usd > 0.0001


def test_settle_twice_or_settle_unknown_is_an_error(budget):
    rid = budget.reserve(GEMINI, 0.01, now=NOW)
    budget.settle(rid, input_tokens=1, output_tokens=1, latency_ms=1)
    with pytest.raises(ValueError):
        budget.settle(rid, input_tokens=1, output_tokens=1, latency_ms=1)
    with pytest.raises(KeyError):
        budget.settle("nope", input_tokens=1, output_tokens=1, latency_ms=1)


def test_concurrent_reservations_cannot_both_pass(tmp_path):
    # Cap 1.0, two threads each try to reserve 0.6 at the same moment: exactly one may win.
    budget = Budget(tmp_path / "ledger.db", cap_usd=1.0)
    start = threading.Barrier(8)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker():
        start.wait()
        try:
            budget.reserve(GEMINI, 0.6, now=NOW)
            result = "ok"
        except BudgetExceeded:
            result = "refused"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 1 and outcomes.count("refused") == 7
    assert budget.month_to_date(NOW) == pytest.approx(0.6)


def test_last_month_does_not_count(budget):
    september = datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)
    spend(budget, 5.0, now=september)
    assert budget.month_to_date(NOW) == 0.0
    assert budget.can_spend(5.0, NOW)
    assert budget.entries(NOW) == []


def test_ledger_survives_restart(tmp_path):
    path = tmp_path / "ledger.db"
    spend(Budget(path, cap_usd=5.0), 3.0)
    assert Budget(path, cap_usd=5.0).month_to_date(NOW) == pytest.approx(3.0)


def test_record_logs_everything_fr6(budget):
    entry = budget.record(
        model=GEMINI, input_tokens=1500, output_tokens=400, latency_ms=1234, request_id="req-1", now=NOW
    )
    assert entry.request_id == "req-1" and entry.status == "settled"
    assert entry.cost_usd == pytest.approx(estimate_cost(GEMINI, 1500, 400))
    [stored] = budget.entries(NOW)
    assert stored == entry
    assert stored.latency_ms == 1234 and stored.ts == NOW


def test_duplicate_request_id_is_rejected(budget):
    spend(budget, 0.0, request_id="dup")
    with pytest.raises(sqlite3.IntegrityError):  # one row per paid call
        budget.reserve(GEMINI, 0.0, request_id="dup", now=NOW)


def test_estimate_cost_uses_price_table():
    price_in, price_out = PRICES_USD_PER_MTOK[GEMINI]
    assert estimate_cost(GEMINI, 1_000_000, 0) == pytest.approx(price_in)
    assert estimate_cost(GEMINI, 0, 1_000_000) == pytest.approx(price_out)


def test_unknown_model_is_priced_conservatively():
    unknown = estimate_cost("some-provider:new-model", 1000, 1000)
    assert unknown >= max(estimate_cost(m, 1000, 1000) for m in PRICES_USD_PER_MTOK)


def test_scan_estimate_is_small_enough_to_afford_many_scans():
    # Sanity check on the whole premise: 5 USD must buy hundreds of scans, even on the paid tier.
    assert 5.0 / estimate_scan_cost(HAIKU) > 500
    assert estimate_scan_cost(GEMINI) < estimate_scan_cost(HAIKU)


def test_cap_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        Budget(tmp_path / "x.db", cap_usd=0)


# --- feature 002: calibration, per-model stats, tiers ------------------------------


def test_estimate_falls_back_to_table_until_enough_settled_rows(budget):
    from app.budget import CALIBRATION_MIN_ROWS, CALIBRATION_SAFETY

    assert budget.estimate_for(GEMINI) == pytest.approx(estimate_scan_cost(GEMINI))
    for _ in range(CALIBRATION_MIN_ROWS - 1):
        spend(budget, 0.001)
    assert budget.estimate_for(GEMINI) == pytest.approx(estimate_scan_cost(GEMINI))  # still too few
    spend(budget, 0.001)
    assert budget.estimate_for(GEMINI) == pytest.approx(0.001 * CALIBRATION_SAFETY)


def test_estimate_tracks_the_worst_recent_call_not_the_mean(budget):
    from app.budget import CALIBRATION_SAFETY

    for cost in (0.001, 0.001, 0.004):
        spend(budget, cost)
    assert budget.estimate_for(GEMINI) == pytest.approx(0.004 * CALIBRATION_SAFETY)


def test_estimate_ignores_failed_zero_cost_and_other_model_rows(budget):
    from app.budget import CALIBRATION_SAFETY

    for _ in range(3):
        spend(budget, 0.002)
    spend(budget, 0.0)  # a free (cached-by-provider?) row must not count as a measurement
    rid = budget.reserve(GEMINI, 2.0, now=NOW)  # big failed row must not skew the mean
    budget.fail(rid)
    budget.record(model=HAIKU, input_tokens=0, output_tokens=0, latency_ms=0, cost_usd=0.9, now=NOW)
    assert budget.estimate_for(GEMINI) == pytest.approx(0.002 * CALIBRATION_SAFETY)
    assert budget.estimate_for(HAIKU) == pytest.approx(estimate_scan_cost(HAIKU))  # only 1 row


def test_estimate_uses_only_the_most_recent_window(budget):
    from app.budget import CALIBRATION_SAFETY, CALIBRATION_WINDOW
    from datetime import timedelta

    for i in range(CALIBRATION_WINDOW + 5):
        spend(budget, 0.1 if i < 5 else 0.001, now=NOW + timedelta(seconds=i))  # old expensive rows first
    assert budget.estimate_for(GEMINI) == pytest.approx(0.001 * CALIBRATION_SAFETY)  # old max fell out of the window


def test_per_model_breakdown_and_escalation_count(budget):
    budget.record(model=GEMINI, input_tokens=0, output_tokens=0, latency_ms=100, cost_usd=0.01, now=NOW)
    budget.record(model=GEMINI, input_tokens=0, output_tokens=0, latency_ms=300, cost_usd=0.03, now=NOW)
    rid = budget.reserve(GEMINI, 0.02, now=NOW)
    budget.fail(rid, latency_ms=999)  # failed latency must not enter the average
    esc = budget.reserve(HAIKU, 0.05, now=NOW, tier="escalation")
    budget.settle(esc, input_tokens=1, output_tokens=1, latency_ms=800, cost_usd=0.05)

    stats = {s.model: s for s in budget.per_model(NOW)}
    assert stats[GEMINI].calls == 3 and stats[GEMINI].failed == 1
    assert stats[GEMINI].cost_usd == pytest.approx(0.06) and stats[GEMINI].avg_latency_ms == 200
    assert stats[HAIKU].calls == 1 and stats[HAIKU].avg_latency_ms == 800
    assert budget.escalations(NOW) == 1
    assert [e.tier for e in budget.entries(NOW)] == ["primary", "primary", "primary", "escalation"]


def test_ledger_from_feature_001_gets_the_tier_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE spend (request_id TEXT PRIMARY KEY, ts TEXT NOT NULL, model TEXT NOT NULL, "
        "input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL, "
        "latency_ms INTEGER NOT NULL, status TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO spend VALUES ('old', ?, ?, 1, 1, 0.01, 5, 'settled')", (NOW.isoformat(), GEMINI))
    conn.commit(); conn.close()
    budget = Budget(path, cap_usd=5.0)
    [entry] = budget.entries(NOW)
    assert entry.tier == "primary" and entry.request_id == "old"
    assert budget.escalations(NOW) == 0


def test_source_column_defaults_to_tap_and_is_migrated_for_old_ledgers(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE spend (request_id TEXT PRIMARY KEY, ts TEXT NOT NULL, model TEXT NOT NULL, "
        "input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL, "
        "latency_ms INTEGER NOT NULL, status TEXT NOT NULL, tier TEXT NOT NULL DEFAULT 'primary')"
    )
    conn.execute("INSERT INTO spend VALUES ('old', ?, ?, 1, 1, 0.01, 5, 'settled', 'primary')", (NOW.isoformat(), GEMINI))
    conn.commit(); conn.close()
    budget = Budget(path, cap_usd=5.0)
    assert budget.entries(NOW)[0].source == "tap"
    budget.reserve(GEMINI, 0.001, now=NOW, source="live")
    esc = budget.reserve(HAIKU, 0.001, now=NOW, tier="escalation", source="live")
    assert budget.scans_by_source(NOW) == {"tap": 1, "live": 1}  # escalation rows are not scans
