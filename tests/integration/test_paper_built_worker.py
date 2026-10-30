"""Installed paper worker acceptance. All broker/data transport and signatures are synthetic.

These tests exercise actual construction, artifact gates, agents, PostgreSQL and
Alpaca submission serialization. They are not observed trading qualification.
"""

import hashlib
import inspect
import json
import os
import subprocess
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests

from agents.context import AgentContext
from agents.runtime import AgentRuntime
from data.config import DataProviderConfig
from infra.postgres import ensure_postgres_schema, migrate_execution_journal, postgres_connection
from ops.agent_bindings import agent_parameters
from ops.artifacts import _FACTORIES
from ops.calendar import USTradingCalendar
from ops.commands import migrate_control_commands
from ops.release_gate import ReleaseIdentity
from ops.runtime_data import RuntimeMarketData, public_provider_config
from ops.worker_builder import WorkerPaths, build_worker
from ops.worker_config import parse_session_controls
from portfolio.accounting import AccountingState
from portfolio.journal import PostgresJournal
from risk.session_store import PostgresSessionRisk
from tests.backtest.test_datasets import manifest, price_record, record, risk_contract
from tests.ops.release_fixtures import paper_release
from tests.ops.test_release_gate import KEY, digest, sign
from tests.portfolio.test_paper_mandate import mandate


class PaperTransport:
    """Only replaces HTTP transport. Order payloads run through the installed adapter."""

    def __init__(self, account, clock):
        self.account, self.clock = account, clock
        self.last, self.bid, self.ask = 101, 100.99, 101.01
        self.posts, self.orders, self.reads = [], {}, []
        self.positions = []

    @staticmethod
    def response(value, status=200):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(value).encode()
        response.headers["Content-Type"] = "application/json"
        return response

    def get(self, url, **kwargs):
        self.reads.append(url)
        at = self.clock().isoformat()
        if url.endswith("/quotes/latest"):
            assert kwargs["params"] == {"feed": "iex"}
            return self.response(
                {"symbol": "SPY", "quote": {"bp": self.bid, "ap": self.ask, "t": at}}
            )
        if url.endswith("/trades/latest"):
            return self.response({"symbol": "SPY", "trade": {"p": self.last, "t": at}})
        if url.endswith("/account/activities"):
            return self.response([])
        if url.endswith("/account"):
            return self.response(
                {
                    "id": self.account,
                    "status": "ACTIVE",
                    "currency": "USD",
                    "cash": "100000",
                    "buying_power": "400000",
                    "trading_blocked": False,
                }
            )
        if url.endswith("/positions"):
            return self.response(self.positions)
        if url.endswith("/clock"):
            return self.response({"is_open": True, "timestamp": at})
        if url.endswith("/orders:by_client_order_id"):
            key = kwargs["params"]["client_order_id"]
            return self.response(self.orders.get(key, {}), 200 if key in self.orders else 404)
        if url.endswith("/orders"):
            return self.response(list(self.orders.values()))
        if "/orders/" in url:
            order = next(
                (item for item in self.orders.values() if item["id"] == url.rsplit("/", 1)[1]), None
            )
            return self.response(order or {}, 200 if order else 404)
        raise AssertionError("unqualified synthetic GET: " + url)

    def post(self, url, **kwargs):
        assert url == "https://paper-api.alpaca.markets/v2/orders"
        payload = dict(kwargs["json"])
        self.posts.append(payload)
        order = {
            **payload,
            "id": str(uuid4()),
            "status": "new",
            "asset_class": "us_equity",
            "filled_qty": "0",
            "filled_avg_price": None,
            "submitted_at": self.clock().isoformat(),
        }
        self.orders[payload["client_order_id"]] = order
        return self.response(order)

    def delete(self, *args, **kwargs):
        raise AssertionError("fixture does not authorize cancellations")


def _authority(args, config, policy, now):
    _, evidence, _ = paper_release(config, args["environment"]["PORTFOLIO_ACCOUNT_ID"], now)
    identity = replace(
        ReleaseIdentity(**evidence["payload"]["identity"]),
        sha=subprocess.check_output(
            ["git", "-C", str(args["checkout"]), "rev-parse", "HEAD"], text=True
        ).strip(),
        policy_hash=policy.content_hash,
        strategy_hash=hashlib.sha256(args["strategy_path"].read_bytes()).hexdigest(),
        data_hash=hashlib.sha256(args["data_path"].read_bytes()).hexdigest(),
    )
    payload = evidence["payload"]
    payload["identity"] = asdict(identity)
    artifacts, replacements = {}, {}
    for old, artifact in payload["artifacts"].items():
        artifact["identity"] = asdict(identity)
        new = digest(artifact)
        artifacts[new], replacements[old] = artifact, new
    payload["artifacts"] = artifacts
    for checks in payload["gates"].values():
        for name, old in checks.items():
            checks[name] = replacements[old]
    args["trust_path"].write_text(
        json.dumps(
            {
                "schema_version": 1,
                "identity": asdict(identity),
                "issuer_key_environment": {"test-reviewer": "OWNER_TEST_KEY"},
                "paper_account_id": identity.account_id,
            }
        )
    )
    args["evidence_path"].write_text(json.dumps(sign(payload)))


@pytest.fixture
def paper_built_worker(
    tmp_path,
    monkeypatch,
    *,
    strict_mandate_simulation=False,
    five_second_trial=False,
    already_observed_opening=False,
):
    dsn = os.environ.get("WORKER_BUILDER_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated WORKER_BUILDER_TEST_POSTGRES_DSN required")
    now = [datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)]
    # An explicitly prepared intraday scenario observes OPEN before elapsed time
    # starts. The original cold-start trial still advances throughout setup.
    clock_anchor = None if already_observed_opening else time.monotonic()
    if five_second_trial:
        strict_mandate_simulation = True

    def clock():
        if (five_second_trial or already_observed_opening) and clock_anchor is not None:
            return now[0] + timedelta(seconds=time.monotonic() - clock_anchor)
        return now[0]

    account = str(uuid4())
    transport = PaperTransport(account, clock)
    monkeypatch.setattr(requests, "get", transport.get)
    monkeypatch.setattr(requests, "post", transport.post)
    monkeypatch.setattr(requests, "delete", transport.delete)

    class BrokerClock(datetime):
        @classmethod
        def now(cls, tz=None):
            observed = clock()
            return observed if tz else observed.replace(tzinfo=None)

    monkeypatch.setattr("portfolio.broker.datetime", BrokerClock)
    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=6)
    migrate_control_commands(dsn, apply=True)
    journal = PostgresJournal(dsn)
    journal.initialize_account(
        account, "paper_broker", AccountingState(Decimal("100000"), Decimal(0), {})
    )
    journal.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=now[0] - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(minutes=1),
    )
    approved = replace(mandate(), account_id=account)
    journal.install_paper_mandate(account, "paper_broker", approved)
    env = {
        "POSTGRES_DSN": dsn,
        "RUNTIME_BACKEND": "postgres",
        "EXECUTION_MODE": "paper_broker",
        "PORTFOLIO_ACCOUNT_ID": account,
        "RUNTIME_NAME": account,
        "ALPACA_API_KEY_ID": "test-key",
        "ALPACA_API_SECRET_KEY": "test-secret",
        "OWNER_TEST_KEY": KEY.decode(),
        "AGENT_ENABLED": "director,quant,risk,compliance,execution,audit",
        "DATA_CACHE_ENABLED": "false",
        "ALERT_STDOUT_ENABLED": "false",
        "EXECUTION_MAX_ORDER_NOTIONAL": "1000",
        "EXECUTION_MAX_ORDER_SHARES": "1",
        "EXECUTION_MAX_SYMBOL_POSITION_SHARES": "1",
    }
    if five_second_trial:
        env["RUNTIME_BUS_DRAIN_TIMEOUT_SECONDS"] = "5"
    if strict_mandate_simulation:
        env.update(EXECUTION_MARKET_HOURS_GUARD="true", EXECUTION_REQUIRE_PAPER_ACCOUNT="true")
    from agents.config import AgentRuntimeConfig

    config = AgentRuntimeConfig.from_env_for_recovery(env)
    calendar = USTradingCalendar()
    dates, day = [], now[0].date() - timedelta(days=1)
    while len(dates) < 80:
        bounds = calendar.session_bounds(day)
        if bounds:
            dates.append(bounds)
        day -= timedelta(days=1)
    prior = dates[0][1]
    metadata_at = now[0] - timedelta(minutes=1) if strict_mandate_simulation else prior
    rows = [price_record(str(close.date()), close.date(), close) for _, close in reversed(dates)]
    rows += [
        record(
            "member",
            "universe",
            event_at=prior.isoformat(),
            available=prior,
            effective_at=dates[-1][0].isoformat(),
            member=True,
        ),
        record(
            "class",
            "risk_classification",
            event_at=metadata_at.isoformat(),
            available=metadata_at,
            asset_type="etf",
        ),
        record(
            "adv",
            "risk_liquidity",
            event_at=metadata_at.isoformat(),
            available=metadata_at,
            average_daily_volume="1000000",
        ),
        record(
            "sectors",
            "etf_sector_map",
            event_at=metadata_at.isoformat(),
            available=metadata_at,
            as_of=metadata_at.date().isoformat(),
            weights={"technology": "0.40", "other": "0.60"},
        ),
    ]
    for row in rows:
        if row["kind"] in {"price", "risk_liquidity"}:
            row["source"] = "alpaca:iex"
    contract = risk_contract()
    contract["policy"] = {}
    if strict_mandate_simulation:
        contract.update(
            freshness_seconds={"mark": 5, "classification": 86400, "liquidity": 86400},
            artifact_ttl_seconds=5,
        )
    research_manifest = manifest(rows, risk_contract=contract)
    if strict_mandate_simulation:
        research_manifest.update(
            created_at=now[0].isoformat(),
            coverage_start=dates[-1][1].date().isoformat(),
            coverage_end=prior.date().isoformat(),
        )
    bundle = tmp_path / "research.json"
    bundle.write_text(json.dumps({"manifest": research_manifest, "records": rows}))
    provider_config = DataProviderConfig.from_env(env)
    data = tmp_path / "data.json"
    data.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "provider": "alpaca_iex",
                "provider_config": public_provider_config(provider_config),
                "research_file": bundle.name,
                "research_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                "quote_policy": {
                    "max_age_seconds": 5,
                    "max_spread_fraction": "0.001",
                    "research_feed": "iex",
                },
            }
        )
    )
    ingestion = RuntimeMarketData.load(data, config=provider_config, now=clock)
    strategy = tmp_path / "strategy.json"
    document = {
        "session_control": {
            "max_mark_age_seconds": 5,
            "boundary_grace_seconds": 2700,
            "window_sessions": 30,
            "max_drawdown": "0.10",
            "control_timeout_seconds": 30,
        },
        "paper_mandate": asdict(approved),
    }
    strategy.write_text(json.dumps(document, default=str))
    args = dict(
        trust_path=tmp_path / "trust.json",
        evidence_path=tmp_path / "evidence.json",
        checkout=Path(inspect.getfile(AgentRuntime)).resolve().parents[2],
        strategy_path=strategy,
        data_path=data,
        environment=env,
        paths=WorkerPaths(
            tmp_path / "performance.json", tmp_path / "audit.jsonl", tmp_path / "reports", account
        ),
        clock=clock,
    )
    if already_observed_opening:
        controls = parse_session_controls(document["session_control"])
        observer = PostgresSessionRisk(
            journal,
            account_id=account,
            mode="paper_broker",
            policy=ingestion.policy,
            max_mark_age=controls.max_mark_age,
            boundary_grace=controls.boundary_grace,
            window_sessions=controls.window_sessions,
            max_drawdown=controls.max_drawdown,
            control_timeout=controls.control_timeout,
            paper_mandate=approved,
        )
        opened_at = now[0]
        opening_market = ingestion.opening_market_inputs(opened_at)
        assert clock() == opened_at and opening_market.as_of == opened_at
        observation = observer.observe(opening_market, now=opened_at)
        assert observer.status() == observation
        assert observation.observed_at == opened_at and observation.checkpoint == 0
        assert observation.experiment is not None
        assert not journal.recovery_required(account, "paper_broker")
        # Start once, before either worker build and either signed authority.
        # Never rebase a decision clock or backdate an already elapsed opening.
        clock_anchor = time.monotonic()
    _authority(args, config, ingestion.policy, now[0])
    prototype = build_worker(**args)
    try:
        parameters = {}
        for name in config.enabled_agents:
            context = AgentContext.build_default(
                name=name,
                ingestion=ingestion,
                extras={
                    **prototype.runtime._agent_extras,
                    "portfolio_store": prototype.runtime.portfolio_store,
                    "broker_adapter": prototype.runtime.broker_adapter,
                    "performance_tracker": prototype.runtime._performance_tracker,
                    "execution_mode": "paper_broker",
                    "execution_safety_config": config.execution_safety,
                    "symbols": ("SPY",),
                    "paper_mandate": approved,
                    "strategy_weights": {"momentum": 1.0},
                    "audit_path": args["paths"].audit,
                    "audit_report_dir": args["paths"].reports,
                },
            ).with_message_bus(prototype.runtime.bus)
            parameters[name] = agent_parameters(_FACTORIES[name](context))
    finally:
        prototype.runtime.stop()
    document.update(
        schema_version=1,
        factories={
            name: {
                "module": _FACTORIES[name].__module__,
                "class": _FACTORIES[name].__name__,
                "source_sha256": hashlib.sha256(
                    Path(inspect.getfile(_FACTORIES[name])).read_bytes()
                ).hexdigest(),
            }
            for name in config.enabled_agents
        },
        strategy_weights={"momentum": 1.0},
        strategy_performance={},
        symbols=["SPY"],
        agent_parameters=parameters,
    )
    strategy.write_text(json.dumps(document, default=str))
    _authority(args, config, ingestion.policy, now[0])
    worker = build_worker(**args)
    try:
        yield SimpleNamespace(
            worker=worker,
            now=now,
            clock=clock,
            transport=transport,
            args=args,
            journal=journal,
            mandate=approved,
        )
    finally:
        worker.runtime.stop()


def _start(state):
    from tests.integration.test_installed_worker import submit

    worker = state.worker
    worker.run_once()
    submit(worker, "start", "start_paper")
    result = worker.run_once()
    assert result["state"] == "succeeded", result.get("details")
    _healthy(state)


def _healthy(state):
    observed = state.worker.runtime.control_readback("start")
    assert observed["state"] == "RUNNING_PAPER", observed
    assert observed["unresolved"] == [], observed
    return observed


def _directives(state):
    with postgres_connection(state.journal.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT payload_json FROM ah_bus_events WHERE account_id=%s "
            "AND mode='paper_broker' AND topic='director.directive' ORDER BY event_id",
            (state.mandate.account_id,),
        )
        return [row[0] for row in cur.fetchall()]


def test_installed_paper_worker_submits_exactly_one_share_at_ask(paper_built_worker):
    state = paper_built_worker
    _start(state)
    assert len(state.transport.posts) == 1
    order = state.transport.posts[0]
    assert order["symbol"] == "SPY" and order["side"] == "buy"
    assert Decimal(order["qty"]) == 1
    assert Decimal(order["limit_price"]) == Decimal("101.01")
    assert order["type"] == "limit" and order["time_in_force"] == "day"
    assert state.journal.snapshot(state.mandate.account_id, "paper_broker").cash == Decimal(
        "100000"
    )
    ticks = state.worker.runtime._tick_count
    directives = _directives(state)
    assert len(directives) == 1
    state.worker.run_once()
    assert state.worker.runtime._tick_count == ticks + 1
    assert len(_directives(state)) == len(directives) + 1
    assert len(_healthy(state)["open_owned_orders"]) == 1
    assert len(state.transport.posts) == 1


def test_installed_paper_worker_does_not_force_a_trade(paper_built_worker):
    state = paper_built_worker
    state.transport.last, state.transport.bid, state.transport.ask = 100, 99.99, 100.01
    _start(state)
    assert len(_directives(state)) == 1
    assert state.transport.posts == []


def test_installed_negative_signal_never_shorts_empty_inventory(paper_built_worker):
    state = paper_built_worker
    state.transport.last, state.transport.bid, state.transport.ask = 99, 98.99, 99.01
    _start(state)
    assert len(_directives(state)) == 1
    assert state.transport.posts == []


def test_installed_unowned_spy_blocks_start_without_adoption(paper_built_worker):
    from tests.integration.test_installed_worker import submit

    state = paper_built_worker
    state.transport.positions = [{"symbol": "SPY", "qty": "77", "asset_class": "us_equity"}]
    worker = state.worker
    worker.run_once()
    submit(worker, "start", "start_paper")
    result = worker.run_once()
    assert result["state"] == "recovery_required", result
    observed = worker.runtime.control_readback("reconcile")
    assert observed["state"] == "RECOVERY_REQUIRED", observed
    assert "positions" in observed["unresolved"], observed
    assert state.transport.positions[0]["qty"] == "77"
    assert state.transport.posts == []
    assert state.journal.snapshot(state.mandate.account_id, "paper_broker").positions == {}
