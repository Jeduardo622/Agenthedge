"""Labeled synthetic Alpaca activity responses; no network or account state."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from portfolio.activities import normalize_v2_nontrade_activity, read_activity_window

NOW = datetime(2026, 9, 14, 21, tzinfo=timezone.utc)


def fill(identifier="activity-1", **changes):
    return {
        "id": identifier,
        "activity_type": "FILL",
        "type": "partial_fill",
        "transaction_time": "2026-09-14T14:00:00.123Z",
        "order_id": "order-1",
        "symbol": "SPY",
        "side": "buy",
        "qty": "1.5",
        "price": "100.01",
        "cum_qty": "1.5",
        "leaves_qty": "2",
        **changes,
    }


def window(pages, **kwargs):
    requests = []

    def fetch(params):
        requests.append(dict(params))
        page = pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    result = read_activity_window(
        fetch,
        account_id="synthetic-account",
        mode="paper_broker",
        after=NOW - timedelta(days=1),
        until=NOW,
        fetched_at=NOW,
        **kwargs,
    )
    return result, requests


def test_original_execution_identity_time_decimal_and_raw_digest():
    result, requests = window([[fill()]])
    assert result.pages_exhausted and result.unresolved == ()
    event = result.events[0]
    assert event.event_id == "activity-1"
    assert event.occurred_at.isoformat() == "2026-09-14T14:00:00.123000+00:00"
    assert event.payload.quantity == Decimal("1.5")
    assert event.payload.price == Decimal("100.01")
    assert event.payload.order_id == "order-1"
    assert len(event.source_hash) == 64
    assert requests[0]["direction"] == "asc"
    assert "activity_types" not in requests[0]


def test_page_token_exhaustion_and_overlap_dedup():
    result, requests = window([[fill("a"), fill("b")], [fill("b"), fill("c")], []], page_size=2)
    assert result.pages_exhausted
    assert [event.event_id for event in result.events] == ["a", "b", "c"]
    assert requests[1]["page_token"] == "b"
    assert requests[2]["page_token"] == "c"
    assert requests[0]["after"] == requests[2]["after"]


@pytest.mark.parametrize(
    "pages",
    [
        [[fill("a")], [fill("a")]],
        [[fill("a")], [fill("a", price="999")]],
        [[fill("a")], TimeoutError("synthetic timeout")],
        [[fill("a")], {"unexpected": "object"}],
    ],
)
def test_incomplete_pages_preserve_uncertainty(pages):
    result, _ = window(pages, page_size=1)
    assert not result.pages_exhausted
    assert result.unresolved


def test_full_last_page_at_budget_is_not_exhaustion():
    result, _ = window([[fill()]], page_size=1, max_pages=1)
    assert not result.pages_exhausted
    assert "page_budget_exhausted" in result.unresolved


@pytest.mark.parametrize(
    "changes",
    [
        {"qty": "NaN"},
        {"qty": "0"},
        {"qty": "-1"},
        {"price": "Infinity"},
        {"side": "short"},
        {"transaction_time": "2026-09-14"},
        {"transaction_time": "2026-09-15T22:00:00Z"},
        {"order_id": ""},
        {"id": ""},
        {"symbol": ""},
        {"type": "bust"},
        {"fee": "1"},
    ],
)
def test_invalid_or_unqualified_economics_never_become_events(changes):
    result, _ = window([[fill(**changes)]])
    assert result.events == ()
    assert result.unresolved


def test_nontrade_date_is_not_fabricated_as_midnight_execution():
    result, _ = window(
        [
            [
                {
                    "id": "dividend-1",
                    "activity_type": "DIV",
                    "date": "2026-09-14",
                    "net_amount": "1.02",
                    "symbol": "SPY",
                }
            ]
        ]
    )
    assert result.pages_exhausted  # Transport exhaustion is not economic reconciliation.
    assert result.events == () and result.unresolved
    assert result.records[0]["date"] == "2026-09-14"


def test_creation_window_does_not_filter_old_execution_time():
    result, _ = window([[fill(transaction_time="2026-09-10T14:00:00Z")]])
    assert result.pages_exhausted and not result.unresolved
    assert result.events[0].occurred_at.day == 10


def test_short_duplicate_page_does_not_prove_exhaustion():
    result, _ = window([[fill(id="first"), fill(id="second")], [fill(id="first")]], page_size=2)
    assert not result.pages_exhausted
    assert "activity_pagination_no_progress" in result.unresolved
    assert len(result.events) == 2


def test_records_are_deeply_immutable_and_isolated():
    raw = fill(metadata={"nested": [1]})
    result, _ = window([[raw]])
    raw["price"] = "999"
    raw["metadata"]["nested"].append(2)
    assert result.records[0]["price"] == "100.01"
    assert result.records[0]["metadata"]["nested"] == (1,)
    with pytest.raises(TypeError):
        result.records[0]["price"] = "5"


@pytest.mark.parametrize("kwargs", [{"page_size": 101}, {"max_pages": 0}, {"page_size": True}])
def test_bad_bounds_rejected_before_fetch(kwargs):
    with pytest.raises(ValueError):
        window([], **kwargs)


def test_sell_is_signed_once_and_empty_window_is_not_fake_fill():
    result, _ = window([[fill(side="sell")]])
    assert result.events[0].payload.quantity == Decimal("-1.5")
    result, _ = window([[]])
    assert result.pages_exhausted and result.events == ()


def test_alpaca_adapter_binds_account_and_reads_all_types_without_redirects(monkeypatch):
    from portfolio.broker import AlpacaPaperBrokerAdapter, BrokerAccount

    adapter = AlpacaPaperBrokerAdapter(
        api_key_id="synthetic-key", api_secret_key="synthetic-secret"
    )
    monkeypatch.setattr(adapter, "get_account", lambda: BrokerAccount("owner", "ACTIVE", True))
    calls = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [fill()]

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("requests.get", get)
    result = adapter.get_activity_window(
        account_id="owner",
        mode="paper_broker",
        after=NOW - timedelta(days=1),
        until=NOW,
        fetched_at=NOW,
    )
    assert result.pages_exhausted and len(result.events) == 1
    assert calls[0][0] == "https://paper-api.alpaca.markets/v2/account/activities"
    assert calls[0][1]["allow_redirects"] is False
    assert "activity_types" not in calls[0][1]["params"]


@pytest.mark.parametrize(
    "actual_id,actual_paper,mode",
    [("other", True, "paper_broker"), ("owner", False, "paper_broker"), ("owner", True, "live")],
)
def test_adapter_rejects_wrong_namespace_before_activity_read(
    monkeypatch, actual_id, actual_paper, mode
):
    from portfolio.broker import AlpacaPaperBrokerAdapter, BrokerAccount

    adapter = AlpacaPaperBrokerAdapter(
        api_key_id="synthetic-key", api_secret_key="synthetic-secret"
    )
    monkeypatch.setattr(
        adapter, "get_account", lambda: BrokerAccount(actual_id, "ACTIVE", actual_paper)
    )
    monkeypatch.setattr("requests.get", lambda *a, **k: pytest.fail("activity read must not occur"))
    with pytest.raises(ValueError, match="account|mode"):
        adapter.get_activity_window(
            account_id="owner", mode=mode, after=NOW - timedelta(days=1), until=NOW, fetched_at=NOW
        )


@pytest.mark.parametrize("status", [302, 429, 503])
def test_adapter_http_failure_cannot_be_empty_complete_window(monkeypatch, status):
    from portfolio.broker import AlpacaPaperBrokerAdapter, BrokerAccount

    adapter = AlpacaPaperBrokerAdapter(
        api_key_id="synthetic-key", api_secret_key="synthetic-secret"
    )
    monkeypatch.setattr(adapter, "get_account", lambda: BrokerAccount("owner", "ACTIVE", True))

    class Response:
        status_code = status

        def raise_for_status(self):
            if status >= 400:
                raise RuntimeError("synthetic HTTP failure")

        def json(self):
            return []

    monkeypatch.setattr("requests.get", lambda *a, **k: Response())
    result = adapter.get_activity_window(
        account_id="owner",
        mode="paper_broker",
        after=NOW - timedelta(days=1),
        until=NOW,
        fetched_at=NOW,
    )
    assert not result.pages_exhausted and result.unresolved == ("activity_read_failed",)


def v2_activity(activity_type="DIV", **changes):
    result = {
        "account_id": "synthetic-account",
        "event_id": "01M2G6X9B80000000000000000",
        "ref_id": "stable-activity-id",
        "activity_type": activity_type,
        "activity_subtype": "CDIV",
        "status": "executed",
        "at": "2026-09-14T15:00:01Z",
        "executed_at": "2026-09-14T15:00:00Z",
        "currency": "USD",
        "net_amount": "1.25",
        "details": {"symbol": "SPY"},
    }
    result.update(changes)
    return result


def test_v2_cash_wrapper_preserves_provider_revision_and_exact_times():
    normalized = normalize_v2_nontrade_activity(
        v2_activity(),
        account_id="synthetic-account",
        mode="paper_broker",
        observed_at=NOW,
    )
    assert normalized.ref_id == "stable-activity-id"
    assert normalized.publication_event_id == "01M2G6X9B80000000000000000"
    assert normalized.published_at.isoformat() == "2026-09-14T15:00:01+00:00"
    assert normalized.business_at.isoformat() == "2026-09-14T15:00:01+00:00"
    assert normalized.event.event_id == "stable-activity-id"
    assert normalized.event.occurred_at.isoformat() == "2026-09-14T15:00:00+00:00"
    assert normalized.event.payload.amount == Decimal("1.25")
    assert normalized.event.payload.reason == "dividend"


def test_business_and_execution_times_are_not_given_undocumented_ordering():
    normalized = normalize_v2_nontrade_activity(
        v2_activity(at="2026-09-14T14:00:00Z", executed_at="2026-09-14T15:00:00Z"),
        account_id="synthetic-account",
        mode="paper_broker",
        observed_at=NOW,
    )
    assert normalized.business_at < normalized.event.occurred_at
    assert normalized.published_at > normalized.event.occurred_at


@pytest.mark.parametrize(
    "record",
    [
        v2_activity(account_id="other"),
        v2_activity(currency="EUR"),
        v2_activity(status="pending"),
        v2_activity(event_id="01M2KHB0R00000000000000000"),
        v2_activity(previous_id="corrected-ref"),
        v2_activity(activity_subtype="DIVFT"),
        v2_activity(net_amount="NaN"),
    ],
)
def test_v2_nontrade_requires_qualified_exact_evidence(record):
    with pytest.raises(ValueError):
        normalize_v2_nontrade_activity(
            record, account_id="synthetic-account", mode="paper_broker", observed_at=NOW
        )


def test_v2_fee_uses_stable_activity_reference_without_inventing_trade_owner():
    normalized = normalize_v2_nontrade_activity(
        v2_activity("FEE", activity_subtype="REG", net_amount="-2", details={"symbol": "SPY"}),
        account_id="synthetic-account",
        mode="paper_broker",
        observed_at=NOW,
    )
    assert normalized.event.payload.reason == "fee"
    assert normalized.event.payload.fee_reference == "alpaca-activity:stable-activity-id"


@pytest.mark.parametrize(
    "activity_type,subtype",
    [
        ("FEE", "OPTION_FEE"),
        ("FEE", "ORF"),
        ("FEE", "OCOM"),
        ("FEE", "LOC"),
        ("FEE", "LCT"),
        ("INT", "FI"),
        ("INT", "unknown"),
    ],
)
def test_v2_fee_and_interest_subtypes_are_explicitly_allowlisted(activity_type, subtype):
    with pytest.raises(ValueError, match="unqualified"):
        normalize_v2_nontrade_activity(
            v2_activity(activity_type, activity_subtype=subtype, net_amount="-1"),
            account_id="synthetic-account",
            mode="paper_broker",
            observed_at=NOW,
        )


def test_v2_split_requires_exact_rates_and_zero_cash():
    normalized = normalize_v2_nontrade_activity(
        v2_activity(
            "SPLIT",
            activity_subtype="FSPLIT",
            net_amount="0",
            details={"symbol": "SPY", "old_rate": "1", "new_rate": "2"},
        ),
        account_id="synthetic-account",
        mode="paper_broker",
        observed_at=NOW,
    )
    assert normalized.event.payload.ratio == Decimal("2")
    with pytest.raises(ValueError):
        normalize_v2_nontrade_activity(
            v2_activity(
                "SPLIT",
                activity_subtype="FSPLIT",
                net_amount="1",
                details={"symbol": "SPY", "old_rate": "1", "new_rate": "2"},
            ),
            account_id="synthetic-account",
            mode="paper_broker",
            observed_at=NOW,
        )


def test_v2_ulid_timestamp_outside_datetime_range_is_consistent_value_error():
    with pytest.raises(ValueError, match="timestamp out of range"):
        normalize_v2_nontrade_activity(
            v2_activity(event_id="7ZZZZZZZZZ0000000000000000"),
            account_id="synthetic-account",
            mode="paper_broker",
            observed_at=NOW,
        )
