from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from backtest.broker import BacktestExecutionConfig, CausalBacktestBrokerAdapter
from portfolio.broker import BrokerOrder
from portfolio.store import PortfolioStore

T = datetime(2026, 9, 14, 20, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.value = T

    def __call__(self):
        return self.value


def broker(tmp_path, **config):
    clock = Clock()
    store = PortfolioStore(tmp_path / "portfolio.json", initial_cash=10000)
    return (
        CausalBacktestBrokerAdapter(store, now=clock, config=BacktestExecutionConfig(**config)),
        store,
        clock,
    )


def order(identifier="one", quantity=10, limit=101):
    return BrokerOrder(identifier, "SPY", quantity, "buy", limit)


def test_order_cannot_fill_until_later_completed_event(tmp_path):
    adapter, store, _ = broker(tmp_path)
    accepted = adapter.submit_order(order())
    adapter.advance(symbol="SPY", event_at=T, close=D("100"), volume=D("1000"))
    assert adapter.get_order_status(accepted.broker_order_id).status == "accepted"
    assert store.snapshot().positions == {}


def test_working_reservations_preserve_pending_quantity_and_worst_price(tmp_path):
    adapter, _, _ = broker(tmp_path)
    adapter.submit_order(order(quantity=10, limit=101))
    reservation = adapter.working_reservations()[0]
    assert reservation.order_id == "one"
    assert reservation.remaining_quantity == D("10")
    assert reservation.worst_price == D("101")
    assert reservation.reserved_buying_power == D("1010")
    assert reservation.state == "accepted"


def test_gap_then_partial_fills_use_shared_fifo_volume_and_exact_fees(tmp_path):
    adapter, store, clock = broker(
        tmp_path,
        spread_bps=D("10"),
        participation_rate=D("0.1"),
        commission_per_share=D("0.1"),
        minimum_commission=D("0.25"),
    )
    first = adapter.submit_order(order("first", 8, 101))
    second = adapter.submit_order(order("second", 8, 101))
    clock.value = T + timedelta(days=1)
    adapter.advance(symbol="SPY", event_at=clock.value, close=D("105"), volume=D("100"))
    assert adapter.get_order_status(first.broker_order_id).status == "accepted"
    clock.value = T + timedelta(days=2)
    adapter.advance(symbol="SPY", event_at=clock.value, close=D("100"), volume=D("100"))
    assert adapter.get_order_status(first.broker_order_id).status == "filled"
    assert adapter.get_order_status(second.broker_order_id).filled_quantity == 2
    assert store.snapshot().positions["SPY"].quantity == 10
    assert [row["commission"] for row in adapter.fill_details] == ["0.8", "0.25"]


def test_pending_order_expires_after_separate_modeled_event_horizon(tmp_path):
    adapter, _, clock = broker(tmp_path, max_eligible_events=2)
    status = adapter.submit_order(order(limit=99))
    clock.value = T + timedelta(days=1)
    adapter.advance(symbol="SPY", event_at=clock.value, close=D("100"), volume=D("100"))
    assert adapter.get_order_status(status.broker_order_id).status == "accepted"
    clock.value = T + timedelta(days=2)
    adapter.advance(symbol="SPY", event_at=clock.value, close=D("100"), volume=D("100"))
    assert adapter.get_order_status(status.broker_order_id).status == "canceled"


def test_latency_and_missing_volume_keep_order_pending(tmp_path):
    adapter, store, clock = broker(tmp_path, latency=timedelta(days=2))
    status = adapter.submit_order(order())
    clock.value = T + timedelta(days=1)
    adapter.advance(symbol="SPY", event_at=clock.value, close=D("100"), volume=D("100"))
    clock.value = T + timedelta(days=2)
    adapter.advance(symbol="SPY", event_at=clock.value, close=D("100"), volume=D("0"))
    assert adapter.get_order_status(status.broker_order_id).status == "accepted"
    assert store.snapshot().positions == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"spread_bps": D("NaN")},
        {"spread_bps": D("20000")},
        {"spread_bps": D("30000")},
        {"commission_per_share": D("-1")},
        {"participation_rate": D("0")},
        {"participation_rate": D("1.1")},
        {"latency": timedelta(seconds=-1)},
        {"max_eligible_events": 0},
    ],
)
def test_invalid_model_configuration_fails_closed(changes):
    with pytest.raises(ValueError):
        BacktestExecutionConfig(**changes)


def test_manifest_is_stable_and_records_all_cost_assumptions():
    config = BacktestExecutionConfig()
    assert config.to_mapping() == config.to_mapping()
    assert config.to_mapping()["price_rule"] == "close_plus_or_minus_half_spread"
    assert len(str(config.to_mapping()["config_hash"])) == 64


def test_duplicate_event_is_noop_and_conflict_fails_closed(tmp_path):
    adapter, store, clock = broker(tmp_path, spread_bps=D("0"), participation_rate=D("1"))
    adapter.submit_order(order(quantity=3))
    clock.value += timedelta(days=1)
    adapter.advance(symbol="spy", event_at=clock.value, close=D("100"), volume=D("1"))
    adapter.advance(symbol="SPY", event_at=clock.value, close=D("100"), volume=D("1"))
    assert store.snapshot().positions["SPY"].quantity == 1
    with pytest.raises(ValueError, match="different economics"):
        adapter.advance(symbol="SPY", event_at=clock.value, close=D("101"), volume=D("1"))


def test_future_and_backward_market_events_fail_before_economics(tmp_path):
    adapter, store, clock = broker(tmp_path, spread_bps=D("0"), participation_rate=D("1"))
    adapter.submit_order(order(quantity=2))
    with pytest.raises(ValueError, match="not yet visible"):
        adapter.advance(
            symbol="SPY", event_at=clock.value + timedelta(days=1), close=D("100"), volume=D("1")
        )
    assert store.snapshot().positions == {}
    clock.value += timedelta(days=2)
    adapter.advance(symbol="SPY", event_at=clock.value, close=D("100"), volume=D("1"))
    with pytest.raises(ValueError, match="monotonically"):
        adapter.advance(
            symbol="SPY", event_at=clock.value - timedelta(days=1), close=D("100"), volume=D("1")
        )
    assert store.snapshot().positions["SPY"].quantity == 1


def test_client_identity_conflict_and_terminal_cancel_are_stable(tmp_path):
    adapter, _, clock = broker(tmp_path, participation_rate=D("1"))
    accepted = adapter.submit_order(order(quantity=1))
    assert adapter.submit_order(order(quantity=1)) == accepted
    with pytest.raises(ValueError, match="different order"):
        adapter.submit_order(order(quantity=2))
    clock.value += timedelta(days=1)
    adapter.advance(symbol="SPY", event_at=clock.value, close=D("100"), volume=D("1"))
    assert adapter.cancel_order(accepted.broker_order_id).status == "filled"


def test_sell_partial_fills_report_vwap_and_signed_economics(tmp_path):
    adapter, store, clock = broker(
        tmp_path,
        spread_bps=D("20"),
        participation_rate=D("1"),
        commission_per_share=D("0.1"),
        minimum_commission=D("0"),
    )
    store.apply_fill(symbol="SPY", quantity=3, price=90)
    status = adapter.submit_order(BrokerOrder("sell", "SPY", 3, "sell", 90))
    clock.value += timedelta(days=1)
    adapter.advance(symbol="SPY", event_at=clock.value, close=D("100"), volume=D("1"))
    clock.value += timedelta(days=1)
    adapter.advance(symbol="SPY", event_at=clock.value, close=D("102"), volume=D("2"))
    final = adapter.get_order_status(status.broker_order_id)
    assert final.status == "filled"
    assert D(str(final.average_fill_price)) == D("101.232")
    assert [event.payload.quantity for event in final.economic_events] == [D("-1"), D("-2")]
    assert store.snapshot().positions == {}
    assert [row["commission"] for row in adapter.fill_details] == ["0.1", "0.2"]


def test_application_failure_poisons_retries_and_new_submissions(tmp_path, monkeypatch):
    adapter, store, clock = broker(tmp_path, spread_bps=D("0"), participation_rate=D("1"))
    adapter.submit_order(order("first", quantity=1))
    adapter.submit_order(order("second", quantity=1))
    original = store.apply_fill
    calls = 0

    def fail_second(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second write failure")
        return original(**kwargs)

    monkeypatch.setattr(store, "apply_fill", fail_second)
    clock.value += timedelta(days=1)
    with pytest.raises(OSError, match="second write"):
        adapter.advance(symbol="SPY", event_at=clock.value, close=D("100"), volume=D("2"))
    assert store.snapshot().positions["SPY"].quantity == 1
    monkeypatch.setattr(store, "apply_fill", original)
    with pytest.raises(RuntimeError, match="recovery required"):
        adapter.advance(symbol="SPY", event_at=clock.value, close=D("100"), volume=D("2"))
    with pytest.raises(RuntimeError, match="recovery required"):
        adapter.submit_order(order("third", quantity=1))
