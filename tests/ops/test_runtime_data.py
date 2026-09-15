"""Actual ingestion with synthetic transport; never observed broker qualification."""

import hashlib
import json
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal

import pytest

from data.config import DataProviderConfig
from ops.runtime_data import RuntimeMarketData, public_provider_config
from tests.backtest.test_datasets import T, manifest, price_record, record, risk_contract


@pytest.fixture
def market(tmp_path, monkeypatch):
    now = [T.replace(hour=13, minute=30)]
    prior = (now[0] - timedelta(days=3)).replace(hour=20, minute=0)
    rows = [
        price_record("prior", prior.date(), prior),
        record(
            "member",
            "universe",
            event_at=prior.isoformat(),
            available=prior,
            effective_at=prior.isoformat(),
            member=True,
        ),
        record(
            "class",
            "risk_classification",
            event_at=prior.isoformat(),
            available=prior,
            asset_type="equity",
            sector="technology",
        ),
        record(
            "adv",
            "risk_liquidity",
            event_at=prior.isoformat(),
            available=prior,
            average_daily_volume="1000000",
        ),
        record("future", "fundamental", value={"pe_ratio": 1}),
    ]
    bundle = tmp_path / "research.json"
    bundle.write_text(
        json.dumps({"manifest": manifest(rows, risk_contract=risk_contract()), "records": rows})
    )
    config = DataProviderConfig(None, "synthetic-test-only", None, None, cache_enabled=False)
    descriptor = tmp_path / "runtime-data.json"
    descriptor.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provider": "finnhub",
                "provider_config": public_provider_config(config),
                "research_file": bundle.name,
                "research_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
            }
        )
    )
    replies = [{"c": 101, "pc": 100, "t": int(now[0].timestamp())}]
    from data.providers.finnhub import finnhub

    class Transport:
        def __init__(self, **kwargs):
            pass

        def quote(self, symbol):
            result = replies[0]
            if isinstance(result, Exception):
                raise result
            return dict(result)

    monkeypatch.setattr(finnhub, "Client", Transport)
    loaded = RuntimeMarketData.load(descriptor, config=config, now=lambda: now[0])
    return loaded, now, replies, descriptor, bundle, config


def test_opening_quote_does_not_require_a_future_completed_daily_bar(market):
    loaded, now, _, _, _, _ = market
    with pytest.raises(ValueError, match="captured"):
        loaded.get_market_snapshot("SPY")
    loaded.refresh(("SPY",))
    quote = loaded.get_market_snapshot("SPY")
    assert quote.quote.last == Decimal("101")
    assert quote.event_at == now[0] and quote.available_at == now[0]
    assert not quote.fundamentals  # Future research is not borrowed from the bundle.
    inputs = loaded.market_inputs(now[0])
    assert inputs.marks["SPY"].value == Decimal("101")
    assert inputs.marks["SPY"].checksum == quote.checksum
    assert inputs.classifications["SPY"].observed_at < now[0]


def test_opening_inputs_preserve_exact_open_when_processing_clock_advances(market):
    loaded, now, _, _, _, _ = market
    venue_open = now[0]
    loaded.refresh(("SPY",))
    now[0] += timedelta(microseconds=25)

    inputs = loaded.opening_market_inputs(now[0])

    assert inputs.as_of == venue_open
    assert inputs.marks["SPY"].observed_at == venue_open
    assert inputs.marks["SPY"].available_at == venue_open


def test_opening_inputs_do_not_relabel_a_later_quote_as_the_open(market):
    loaded, now, replies, _, _, _ = market
    venue_open = now[0]
    now[0] += timedelta(seconds=1)
    replies[0] = {"c": 101, "pc": 100, "t": int(now[0].timestamp())}
    loaded.refresh(("SPY",))

    inputs = loaded.opening_market_inputs(now[0])

    assert inputs.as_of == venue_open
    assert inputs.marks == {}


def test_failed_refresh_removes_old_quotes_and_never_falls_back_to_daily_close(market):
    loaded, now, replies, _, _, _ = market
    loaded.refresh(("SPY",))
    replies[0] = RuntimeError("synthetic-unavailable")
    now[0] += timedelta(seconds=1)
    loaded.refresh(("SPY",))
    assert loaded.market_inputs(now[0]).marks == {}
    with pytest.raises(ValueError, match="captured"):
        loaded.get_market_snapshot("SPY")


def test_capture_is_not_backdated_to_a_decision_before_receipt(market):
    loaded, now, _, _, _, _ = market
    loaded.refresh(("SPY",))
    assert loaded.market_inputs(now[0] - timedelta(microseconds=1)).marks == {}
    now[0] += timedelta(seconds=301)
    assert loaded.market_inputs(now[0]).marks == {}
    with pytest.raises(ValueError, match="stale"):
        loaded.get_market_snapshot("SPY")


def test_mutated_research_provider_config_and_provider_instance_are_rejected(market):
    loaded, _, _, descriptor, bundle, config = market
    loaded.require()
    original = bundle.read_bytes()
    bundle.write_bytes(original + b" ")
    with pytest.raises(ValueError, match="research"):
        loaded.require()
    bundle.write_bytes(original)
    loaded.provider.config = replace(config, data_quote_freshness_seconds=9999)
    with pytest.raises(ValueError, match="provider"):
        loaded.require()
    loaded.provider.config = config
    loaded.provider._providers["finnhub"] = object()
    with pytest.raises(ValueError, match="provider"):
        loaded.require()


def test_provider_descriptor_never_serializes_credentials_or_accepts_a_different_config(market):
    _, now, _, descriptor, _, config = market
    assert "synthetic-test-only" not in descriptor.read_text()
    assert set(asdict(config)) - set(public_provider_config(config)) == {
        "alpha_vantage_key",
        "finnhub_key",
        "fred_api_key",
        "news_api_key",
        "alpaca_api_key_id",
        "alpaca_api_secret_key",
    }
    with pytest.raises(ValueError, match="provider"):
        RuntimeMarketData.load(
            descriptor, config=replace(config, cache_enabled=True), now=lambda: now[0]
        )


def test_research_path_cannot_escape_descriptor_directory(market):
    _, now, _, descriptor, _, config = market
    value = json.loads(descriptor.read_text())
    value["research_file"] = "../research.json"
    descriptor.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="research"):
        RuntimeMarketData.load(descriptor, config=config, now=lambda: now[0])


def test_replaced_risk_metadata_provider_is_rejected(market):
    loaded, _, _, _, _, _ = market
    loaded._reference_market = lambda at: None
    with pytest.raises(ValueError, match="provider"):
        loaded.require()


def test_failed_integrity_refresh_revokes_previously_captured_quotes(market):
    loaded, now, _, _, bundle, _ = market
    loaded.refresh(("SPY",))
    bundle.write_bytes(bundle.read_bytes() + b" ")
    with pytest.raises(ValueError, match="research"):
        loaded.refresh(("SPY",))
    with pytest.raises(ValueError):
        loaded.get_market_snapshot("SPY")
    with pytest.raises(ValueError):
        loaded.market_inputs(now[0])


def test_risk_artifact_freshness_cannot_exceed_provider_freshness(market):
    loaded, _, _, _, _, _ = market
    assert loaded.thresholds.mark == timedelta(seconds=300)


@pytest.mark.parametrize("ratio,current", [("2", 50), ("0.5", 200)])
def test_actual_director_reference_does_not_turn_a_split_into_a_price_return(
    market, tmp_path, ratio, current
):
    from agents.context import AgentContext
    from agents.impl.director import DirectorAgent
    from agents.messaging import MessageBus
    from backtest.datasets import records_checksum
    from portfolio.store import PortfolioStore

    _, now, replies, descriptor, bundle, config = market
    payload = json.loads(bundle.read_text())
    payload["records"][0]["reference_close"] = str(current)
    payload["records"].append(
        record(
            "split",
            "corporate_action",
            event_at=now[0].isoformat(),
            available=now[0],
            action_type="split",
            ratio=ratio,
            effective_at=now[0].isoformat(),
        )
    )
    payload["manifest"]["records_checksum"] = records_checksum(tuple(payload["records"]))
    bundle.write_text(json.dumps(payload))
    approved = json.loads(descriptor.read_text())
    approved["research_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
    descriptor.write_text(json.dumps(approved))
    replies[0] = {"c": current, "pc": 100, "t": int(now[0].timestamp())}
    loaded = RuntimeMarketData.load(descriptor, config=config, now=lambda: now[0])
    loaded.refresh(("SPY",))
    bus = MessageBus()
    outputs = []
    bus.subscribe(
        lambda event: outputs.append(dict(event.message.payload)), topics=["director.directive"]
    )
    context = AgentContext.build_default(
        name="director",
        ingestion=loaded,
        extras={
            "portfolio_store": PortfolioStore(tmp_path / "director.json"),
            "symbols": ["SPY"],
            "now": lambda: now[0],
        },
    ).with_message_bus(bus)
    agent = DirectorAgent(context)
    try:
        agent.tick()
        assert bus.drain(1)
        assert len(outputs) == 1
        assert outputs[0]["quote"]["pc"] == 100
        assert outputs[0]["quote"]["c"] == current
        assert outputs[0]["quote"]["reference_c"] == current
        assert outputs[0]["quote"]["reference_pc"] == current
    finally:
        bus.close()
