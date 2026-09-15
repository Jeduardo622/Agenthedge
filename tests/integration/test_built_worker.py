"""Actual builder and installed control path; only transport and clock are synthetic."""

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pytest

from agents.config import AgentRuntimeConfig
from ops.worker_builder import WorkerPaths, build_worker
from tests.integration.test_installed_worker import installed_worker, submit  # noqa: F401
from tests.ops.test_release_gate import KEY


@pytest.fixture(autouse=True)
def explicit_fixture_backend(monkeypatch):
    monkeypatch.setenv("RUNTIME_BACKEND", "postgres")


@pytest.fixture
def built_worker(installed_worker, tmp_path, monkeypatch):  # noqa: F811
    seed, current, _, strategy, data, _ = installed_worker
    seed.runtime.stop()
    account = seed.store.account_id
    roster = "director,quant,risk,compliance,execution,audit"
    environment = {
        "POSTGRES_DSN": seed.store.dsn,
        "RUNTIME_BACKEND": "postgres",
        "EXECUTION_MODE": "paper_broker",
        "PORTFOLIO_ACCOUNT_ID": account,
        "RUNTIME_NAME": account,
        "AGENT_ENABLED": roster,
        "AGENT_PIPELINE": roster,
        "FINNHUB_API_KEY": "synthetic-test-only",
        "DATA_CACHE_ENABLED": "false",
        "ALPHA_VANTAGE_RETRY_DELAY_SECONDS": "1",
        "ALPHA_VANTAGE_RATE_LIMIT_BACKOFF_SECONDS": "5",
        "ALPACA_API_KEY_ID": "synthetic",
        "ALPACA_API_SECRET_KEY": "synthetic",
        "OWNER_TEST_KEY": KEY.decode(),
        "EXECUTION_MAX_ORDER_NOTIONAL": str(
            seed.runtime.config.execution_safety.max_order_notional
        ),
        "EXECUTION_MAX_ORDER_SHARES": str(seed.runtime.config.execution_safety.max_order_shares),
        "EXECUTION_MAX_SYMBOL_POSITION_SHARES": str(
            seed.runtime.config.execution_safety.max_symbol_position_shares
        ),
    }
    assert AgentRuntimeConfig.from_env_for_recovery(environment) == seed.runtime.config
    trust_path, evidence_path = tmp_path / "owner.json", tmp_path / "candidate.json"
    trust_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "identity": asdict(seed.trust.expected),
                "issuer_key_environment": {"test-reviewer": "OWNER_TEST_KEY"},
                "paper_account_id": seed.trust.paper_account_id,
            }
        )
    )
    evidence_path.write_text(seed.runtime._release_authorization._evidence_json)
    calls = []
    from data.providers.finnhub import finnhub

    transport_quote = finnhub.Client.quote

    def quote(client, symbol):
        calls.append("synthetic-provider-quote")
        return transport_quote(client, symbol)

    monkeypatch.setattr(finnhub.Client, "quote", quote)

    class Response:
        status_code = 200

        def __init__(self, value):
            self.value = value

        def raise_for_status(self):
            pass

        def json(self):
            return self.value

    def get(url, **kwargs):
        path = urlparse(url).path
        calls.append(path)
        if path == "/v2/account":
            return Response({"id": account, "currency": "USD", "cash": "1000", "status": "ACTIVE"})
        if path in {"/v2/positions", "/v2/orders", "/v2/account/activities"}:
            return Response([])
        if path == "/v2/clock":
            return Response({"is_open": True, "timestamp": current[0].isoformat()})
        pytest.fail(f"unexpected synthetic GET path: {path}")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0] if tz is not None else current[0].replace(tzinfo=None)

    monkeypatch.setattr("portfolio.broker.datetime", Clock)
    monkeypatch.setattr("portfolio.broker.requests.get", get)
    monkeypatch.setattr(
        "portfolio.broker.requests.post", lambda *a, **k: pytest.fail("broker POST")
    )
    monkeypatch.setattr(
        "portfolio.broker.requests.delete", lambda *a, **k: pytest.fail("broker DELETE")
    )
    monkeypatch.setenv("RUN_ID", "ambient-id-must-not-be-adopted")
    paths = WorkerPaths(
        tmp_path / "built-learning.json",
        tmp_path / "built-audit.jsonl",
        tmp_path / "built-reports",
        account + "-explicit",
    )
    worker = build_worker(
        trust_path=trust_path,
        evidence_path=evidence_path,
        checkout=Path(__file__).resolve().parents[2],
        strategy_path=strategy,
        data_path=data,
        environment=environment,
        paths=paths,
        clock=lambda: current[0],
    )
    try:
        yield worker, calls, strategy, paths
    finally:
        worker.runtime.stop()


def test_actual_builder_constructs_without_activation_then_explicit_start(built_worker):
    worker, calls, _, paths = built_worker
    assert worker.lease is None
    assert worker.runtime._agents == []
    assert worker.runtime._tick_count == 0
    assert worker.runtime.ingestion._quotes == {}
    assert calls == []
    submit(worker, "built-start", "start_paper")
    result = worker.run_once()
    assert result["state"] == "succeeded", result
    assert worker.runtime._tick_count == 1
    assert len(worker.runtime._agents) == 6
    assert all(agent.context.run_id == paths.instance_id for agent in worker.runtime._agents)
    assert worker.store.running_observation(release=worker.trust.expected.sha)
    assert "/v2/account" in calls
    assert "synthetic-provider-quote" in calls


def test_built_worker_artifact_mutation_blocks_new_ticks(built_worker):
    worker, _, strategy, _ = built_worker
    submit(worker, "built-start", "start_paper")
    result = worker.run_once()
    assert result["state"] == "succeeded", result
    ticks = worker.runtime._tick_count
    strategy.write_bytes(strategy.read_bytes() + b" ")
    worker.run_once()
    assert worker.runtime._tick_count == ticks
    assert worker.store.running_observation(release=worker.trust.expected.sha) is None
