"""Installed SIP mandate acceptance on an explicitly disposable PostgreSQL fixture.

All account, issuer, market and order responses are synthetic; this is not live proof.
"""

from decimal import Decimal

import pytest
import requests

from tests.integration.test_paper_built_worker import _start, paper_built_worker


@pytest.fixture
def paper_sip_worker(tmp_path, monkeypatch):
    # Seed SIP before construction instead of rebuilding a stopped IEX namespace.
    fixture = paper_built_worker.__wrapped__(tmp_path, monkeypatch, feed="sip")
    state = next(fixture)
    feed_calls = []
    original = state.transport.get

    def get(url, **options):
        if "data.alpaca.markets" in url:
            feed_calls.append((url, options))
            assert options["params"] == {"feed": "sip"}
        return original(url, **options)

    monkeypatch.setattr(requests, "get", get)
    try:
        yield state, feed_calls
    finally:
        fixture.close()


def test_installed_sip_paper_worker_preserves_one_share_day_limit_mandate(paper_sip_worker):
    state, calls = paper_sip_worker
    assert state.worker.runtime._bus_drain_timeout_seconds == 2.0
    _start(state)
    assert calls
    assert all(options["params"] == {"feed": "sip"} for _, options in calls)
    assert len(state.transport.posts) == 1
    order = state.transport.posts[0]
    assert order["symbol"] == "SPY" and order["side"] == "buy"
    assert order["type"] == "limit" and order["time_in_force"] == "day"
    assert Decimal(order["qty"]) == 1
    assert Decimal(order["limit_price"]) == Decimal("101.01")
    assert state.journal.snapshot(state.mandate.account_id, "paper_broker").cash == Decimal(
        "100000"
    )
