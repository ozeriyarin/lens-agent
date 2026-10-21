"""Budget guard tests (constitution II and V).

The guard is the one piece of code that stands between us and a surprise
bill, so the tests pin down the boundary behaviour: exactly-at-cap is allowed,
one cent over is refused, last month's spend does not count, and the ledger
survives a restart.
"""

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


def test_fresh_ledger_has_no_spend(budget):
    assert budget.month_to_date(NOW) == 0.0
    assert budget.remaining(NOW) == 5.0
    assert budget.can_spend(0.01, NOW)


def test_record_adds_to_month_to_date(budget):
    budget.record(model=GEMINI, input_tokens=1000, output_tokens=0, latency_ms=10, cost_usd=1.25, now=NOW)
    budget.record(model=GEMINI, input_tokens=1000, output_tokens=0, latency_ms=10, cost_usd=0.75, now=NOW)
    assert budget.month_to_date(NOW) == pytest.approx(2.0)
    assert budget.remaining(NOW) == pytest.approx(3.0)


def test_exactly_at_cap_is_allowed_one_cent_over_is_not(budget):
    budget.record(model=GEMINI, input_tokens=0, output_tokens=0, latency_ms=0, cost_usd=4.0, now=NOW)
    assert budget.can_spend(1.0, NOW)  # 4 + 1 == 5, FR-5 says <= cap
    assert not budget.can_spend(1.01, NOW)


def test_check_refuses_loudly_with_the_numbers(budget):
    budget.record(model=HAIKU, input_tokens=0, output_tokens=0, latency_ms=0, cost_usd=4.99, now=NOW)
    budget.check(0.01, NOW)  # fine
    with pytest.raises(BudgetExceeded) as err:
        budget.check(0.02, NOW)
    assert "cap 5.00" in str(err.value) and "4.9900" in str(err.value)


def test_last_month_does_not_count(budget):
    september = datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)
    budget.record(model=GEMINI, input_tokens=0, output_tokens=0, latency_ms=0, cost_usd=5.0, now=september)
    assert budget.month_to_date(NOW) == 0.0
    assert budget.can_spend(5.0, NOW)
    assert [e.cost_usd for e in budget.entries(NOW)] == []


def test_ledger_survives_restart(tmp_path):
    path = tmp_path / "ledger.db"
    Budget(path, cap_usd=5.0).record(model=GEMINI, input_tokens=0, output_tokens=0, latency_ms=0, cost_usd=3.0, now=NOW)
    reopened = Budget(path, cap_usd=5.0)
    assert reopened.month_to_date(NOW) == pytest.approx(3.0)


def test_record_logs_everything_fr6(budget):
    entry = budget.record(
        model=GEMINI, input_tokens=1500, output_tokens=400, latency_ms=1234, request_id="req-1", now=NOW
    )
    assert entry.request_id == "req-1"
    assert entry.cost_usd == pytest.approx(estimate_cost(GEMINI, 1500, 400))
    [stored] = budget.entries(NOW)
    assert stored == entry
    assert stored.latency_ms == 1234 and stored.ts == NOW


def test_duplicate_request_id_is_rejected(budget):
    budget.record(model=GEMINI, input_tokens=0, output_tokens=0, latency_ms=0, request_id="dup", now=NOW)
    with pytest.raises(Exception):  # sqlite IntegrityError: one row per paid call
        budget.record(model=GEMINI, input_tokens=0, output_tokens=0, latency_ms=0, request_id="dup", now=NOW)


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
