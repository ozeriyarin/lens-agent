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

GEMINI = "google-gla:gemini-2.5-flash"
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
