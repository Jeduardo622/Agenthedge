"""Installed SIP mandate acceptance on an explicitly disposable PostgreSQL fixture.

All account, issuer, market and order responses are synthetic; this is not live proof.
"""

import hashlib
import json
from decimal import Decimal

import pytest
import requests

from agents.config import AgentRuntimeConfig
from backtest.datasets import records_checksum
from data.config import DataProviderConfig
from ops.runtime_data import RuntimeMarketData
from ops.worker_builder import build_worker
from tests.integration.test_paper_built_worker import paper_built_worker  # noqa: F401
from tests.integration.test_paper_built_worker import _authority, _start


@pytest.fixture
def paper_sip_worker(paper_built_worker, monkeypatch):  # noqa: F811
    state = paper_built_worker
    state.worker.runtime.stop()
    data = state.args["data_path"]
    descriptor = json.loads(data.read_text())
    research = data.parent / descriptor["research_file"]
    bundle = json.loads(research.read_text())
    for row in bundle["records"]:
        if row["kind"] in {"price", "risk_liquidity"}:
            row["source"] = "alpaca:sip"
    bundle["manifest"]["records_checksum"] = records_checksum(bundle["records"])
    research.write_text(json.dumps(bundle))
    descriptor.update(
        schema_version=3,
        provider="alpaca_sip",
        research_sha256=hashlib.sha256(research.read_bytes()).hexdigest(),
    )
    descriptor["quote_policy"]["research_feed"] = "sip"
    data.write_text(json.dumps(descriptor))
    feed_calls = []

    def get(url, **options):
        if "data.alpaca.markets" not in url:
            return state.transport.get(url, **options)
        feed_calls.append((url, options))
        assert options["params"] == {"feed": "sip"}
        at = state.clock().isoformat()
        value = (
            {"quote": {"bp": state.transport.bid, "ap": state.transport.ask, "t": at}}
            if url.endswith("/quotes/latest")
            else {"trade": {"p": state.transport.last, "t": at}}
        )
        return state.transport.response({"symbol": "SPY", **value})

    monkeypatch.setattr(requests, "get", get)
    environment = state.args["environment"]
    ingestion = RuntimeMarketData.load(
        data, config=DataProviderConfig.from_env(environment), now=state.clock
    )
    _authority(
        state.args,
        AgentRuntimeConfig.from_env_for_recovery(environment),
        ingestion.policy,
        state.clock(),
    )
    state.worker = build_worker(**state.args)
    try:
        yield state, feed_calls
    finally:
        state.worker.runtime.stop()


def test_installed_sip_paper_worker_preserves_one_share_day_limit_mandate(paper_sip_worker):
    state, calls = paper_sip_worker
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
