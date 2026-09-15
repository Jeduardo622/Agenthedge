from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from portfolio.reconciliation import economic_snapshot, read_order_window

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def order(identifier="o", **changes):
    return dict(
        id=identifier,
        client_order_id="c-" + identifier,
        symbol="SPY",
        side="buy",
        qty="2",
        filled_qty="1",
        filled_avg_price="100",
        status="partially_filled",
        submitted_at=NOW.isoformat(),
        asset_class="us_equity",
        **changes,
    )


def test_order_id_pagination_keeps_tied_submission_times():
    requests = []
    pages = [[order("a"), order("b")], [order("c")]]

    def fetch(params):
        requests.append(params)
        return pages.pop(0)

    result = read_order_window(
        fetch,
        account_id="a",
        mode="paper_broker",
        observed_at=NOW,
        scope="all",
        after=NOW - timedelta(days=1),
        page_size=2,
    )
    assert result.pages_exhausted and not result.unresolved
    assert len(result.orders) == 3
    assert requests[1]["before_order_id"] == "b"
    assert "after" not in requests[1] and "until" not in requests[1]


@pytest.mark.parametrize(
    "pages",
    [
        [[order("a")], [order("a")]],
        [[order("a")], TimeoutError()],
        [[dict(order(), qty="NaN")]],
        [[dict(order(), status="surprise")]],
    ],
)
def test_bad_order_coverage_never_complete(pages):
    def fetch(_):
        item = pages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    result = read_order_window(
        fetch,
        account_id="a",
        mode="paper_broker",
        observed_at=NOW,
        scope="open",
        page_size=1,
        max_pages=2,
    )
    assert result.unresolved
    assert not result.pages_exhausted


def test_strict_snapshot_preserves_decimal_and_rejects_missing_cash():
    result = economic_snapshot(
        {"id": "a", "cash": "100.123456789123456789", "currency": "USD"},
        [{"symbol": "SPY", "qty": "1.000000000000000001", "asset_class": "us_equity"}],
        account_id="a",
        mode="paper_broker",
        observed_at=NOW,
    )
    assert result.cash == D("100.123456789123456789")
    assert result.positions["SPY"] == D("1.000000000000000001")
    with pytest.raises(ValueError):
        economic_snapshot({"id": "a"}, [], account_id="a", mode="paper_broker", observed_at=NOW)


@pytest.mark.parametrize(
    "positions",
    [
        [{"symbol": "SPY", "qty": "NaN"}],
        [{"symbol": "SPY", "qty": "1"}, {"symbol": "SPY", "qty": "2"}],
        [{"symbol": "", "qty": "1"}],
    ],
)
def test_invalid_positions_fail_closed(positions):
    with pytest.raises(ValueError):
        economic_snapshot(
            {"id": "a", "cash": "100", "currency": "USD"},
            positions,
            account_id="a",
            mode="paper_broker",
            observed_at=NOW,
        )


def test_order_window_501_and_old_open_order():
    pages = [
        [order(str(i)) for i in range(500)],
        [dict(order("old"), submitted_at=(NOW - timedelta(days=30)).isoformat())],
    ]
    result = read_order_window(
        lambda _: pages.pop(0), account_id="a", mode="paper_broker", observed_at=NOW, scope="open"
    )
    assert result.pages_exhausted and len(result.orders) == 501


def test_adapter_strict_reads_use_actual_endpoints_without_post():
    from portfolio.broker import AlpacaPaperBrokerAdapter

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    adapter = object.__new__(AlpacaPaperBrokerAdapter)
    adapter._base_url = "https://paper-api.alpaca.markets"
    adapter._headers = {}
    adapter._timeout_seconds = 1
    paths = []

    def get(url, **kwargs):
        paths.append((url, kwargs.get("params")))
        if url.endswith("/account"):
            return Response({"id": "a", "cash": "1000.001", "currency": "USD", "status": "ACTIVE"})
        if url.endswith("/positions"):
            return Response([])
        if url.endswith("by_client_order_id"):
            return Response(order("o"))
        return Response([])

    adapter._safe_get = get
    assert adapter.get_reconciliation_order(
        "c-o", account_id="a", mode="paper_broker"
    ).quantity == D(2)
    assert adapter.get_economic_snapshot(account_id="a", mode="paper_broker").cash == D("1000.001")
    assert adapter.get_order_window(
        account_id="a", mode="paper_broker", scope="all"
    ).pages_exhausted
    assert any(
        path.endswith("by_client_order_id") and params == {"client_order_id": "c-o"}
        for path, params in paths
    )
    with pytest.raises(ValueError):
        adapter.get_economic_snapshot(account_id="other", mode="paper_broker")


def test_out_of_order_provider_page_cannot_prove_lower_bound_coverage():
    old = dict(order("old"), submitted_at=(NOW - timedelta(days=30)).isoformat())
    result = read_order_window(
        lambda _: [old, order("new"), dict(old, id="old2")],
        account_id="a",
        mode="paper_broker",
        observed_at=NOW,
        scope="all",
        after=NOW - timedelta(days=1),
        page_size=3,
        max_pages=1,
    )
    assert not result.pages_exhausted


def test_short_page_with_only_non_last_duplicate_is_not_exhaustion():
    now = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)

    def order(i):
        return dict(
            id=i,
            client_order_id=i,
            symbol="SPY",
            side="buy",
            qty="1",
            filled_qty="0",
            status="new",
            asset_class="us_equity",
            submitted_at=now.isoformat(),
        )

    pages = [[order("a"), order("b")], [order("a")]]
    result = read_order_window(
        lambda _: pages.pop(0),
        account_id="a",
        mode="paper_broker",
        observed_at=now,
        scope="open",
        page_size=2,
    )
    assert not result.pages_exhausted
