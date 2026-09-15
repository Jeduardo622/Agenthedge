from contextlib import contextmanager
from pathlib import Path

import pytest

from agents.config import AgentRuntimeConfig
from agents.postgres_bus import PostgresMessageBus
from agents.registry import AgentRegistry
from agents.runtime import AgentRuntime
from audit import JsonlAuditSink
from learning.performance import PerformanceTracker
from portfolio.store import PortfolioStore


def test_runtime_explicit_paths_and_identity_ignore_ambient_defaults(tmp_path, monkeypatch):
    tracker = PerformanceTracker(tmp_path / "chosen-learning.json")
    monkeypatch.setenv("PERFORMANCE_TRACKER_PATH", str(tmp_path / "forbidden" / "learning.json"))
    monkeypatch.setenv("AUDIT_REPORT_DIR", str(tmp_path / "forbidden" / "reports"))
    monkeypatch.setenv("RUN_ID", "ambient-wrong")
    runtime = AgentRuntime(
        registry=AgentRegistry(),
        ingestion=object(),
        config=AgentRuntimeConfig(),
        portfolio_store=PortfolioStore(tmp_path / "portfolio.json"),
        audit_sink=JsonlAuditSink(tmp_path / "audit.jsonl"),
        performance_tracker=tracker,
        audit_report_dir=tmp_path / "chosen-reports",
        instance_id="chosen-instance",
    )
    try:
        assert runtime._performance_tracker is tracker
        assert runtime._audit_report_dir == tmp_path / "chosen-reports"
        assert runtime._runtime_instance_id == "chosen-instance"
        assert not (tmp_path / "forbidden").exists()
    finally:
        runtime.stop()


def test_bus_existing_schema_mode_does_not_issue_ddl(monkeypatch):
    statements = []

    class Cursor:
        def execute(self, sql, params=None):
            statements.append(sql)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def connection(dsn):
        yield Connection()

    monkeypatch.setattr("agents.postgres_bus.postgres_connection", connection)
    monkeypatch.setattr("agents.postgres_bus.ensure_postgres_schema", lambda _: pytest.fail("DDL"))
    bus = PostgresMessageBus("synthetic", initialize_schema=False)
    assert statements
    assert all(sql.strip().upper().startswith(("SELECT", "SET TRANSACTION")) for sql in statements)
    bus.close()


def test_builder_preserves_explicit_alert_configuration_without_sending(construction, monkeypatch):
    from observability.alerts import WebhookTransport
    from ops.worker_builder import build_worker

    args, _, _ = construction
    webhook = "https://alerts.synthetic.invalid/hook/synthetic-token"
    args["environment"].update(
        ALERT_WEBHOOK_URL=webhook,
        ALERT_WEBHOOK_TIMEOUT_SECONDS="7",
        ALERT_STDOUT_ENABLED="false",
        ALERT_MIN_SEVERITY="critical",
        ALERT_SEVERITY_RISK_REJECT="warning",
    )
    monkeypatch.setattr(WebhookTransport, "send", lambda *args: pytest.fail("webhook send"))
    monkeypatch.setattr(
        "observability.alerts.requests.post", lambda *args, **kw: pytest.fail("HTTP")
    )
    worker = build_worker(**args)
    try:
        notifier = worker.runtime.alert_notifier
        assert notifier.min_severity == "critical"
        assert notifier.action_severities["risk_reject"] == "warning"
        assert len(notifier._transports) == 1
        transport = notifier._transports[0]
        assert type(transport) is WebhookTransport
        assert transport.url == webhook
        assert transport.timeout_seconds == 7
        assert "synthetic-token" not in repr(notifier)
        assert "synthetic-token" not in repr(transport)
    finally:
        worker.runtime.stop()


def test_builder_api_requires_explicit_paths():
    from ops.worker_builder import WorkerPaths

    with pytest.raises(ValueError):
        WorkerPaths(Path("relative.json"), Path("/tmp/audit.json"), Path("/tmp/reports"), "")


@pytest.fixture
def construction(tmp_path, monkeypatch):
    import hashlib
    import json
    import os
    from dataclasses import asdict, replace
    from datetime import timedelta
    from decimal import Decimal
    from uuid import uuid4

    from infra.postgres import ensure_postgres_schema, migrate_execution_journal
    from ops.commands import migrate_control_commands
    from portfolio.accounting import AccountingState
    from portfolio.journal import PostgresJournal
    from tests.ops.test_release_gate import IDENTITY, KEY, dossier, sign
    from tests.ops.test_runtime_data import market

    dsn = os.environ.get("WORKER_BUILDER_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated WORKER_BUILDER_TEST_POSTGRES_DSN required")
    loaded, now, _, data, _, _ = market.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(
        type(loaded._client), "quote", lambda *args: pytest.fail("constructor quote request")
    )
    ensure_postgres_schema(dsn)
    migrate_execution_journal(dsn, apply=True, target_version=6)
    migrate_control_commands(dsn, apply=True)
    account = "builder-" + uuid4().hex
    journal = PostgresJournal(dsn)
    journal.initialize_account(
        account, "paper_broker", AccountingState(Decimal(1000), Decimal(0), {})
    )
    journal.initialize_reconciliation(
        account,
        "paper_broker",
        bootstrap_after=now[0] - timedelta(days=1),
        overlap=timedelta(hours=1),
        max_observation=timedelta(minutes=1),
    )
    env = {
        "POSTGRES_DSN": dsn,
        "RUNTIME_BACKEND": "postgres",
        "EXECUTION_MODE": "paper_broker",
        "PORTFOLIO_ACCOUNT_ID": account,
        "RUNTIME_NAME": account,
        "FINNHUB_API_KEY": "synthetic-test-only",
        "DATA_CACHE_ENABLED": "false",
        "ALPHA_VANTAGE_RETRY_DELAY_SECONDS": "1",
        "ALPHA_VANTAGE_RATE_LIMIT_BACKOFF_SECONDS": "5",
        "ALPACA_API_KEY_ID": "synthetic",
        "ALPACA_API_SECRET_KEY": "synthetic",
        "OWNER_TEST_KEY": KEY.decode(),
        "AGENT_ENABLED": "director,quant,risk,compliance,execution,audit",
    }
    config = AgentRuntimeConfig.from_env_for_recovery(env)
    strategy = tmp_path / "strategy.json"
    strategy.write_text(
        json.dumps(
            {
                "session_control": dict(
                    max_mark_age_seconds=30,
                    boundary_grace_seconds=2700,
                    window_sessions=30,
                    max_drawdown="0.10",
                    control_timeout_seconds=30,
                )
            }
        )
    )
    identity = replace(
        IDENTITY,
        account_id=account,
        mode="paper_broker",
        config_hash=config.release_config_hash(),
        policy_hash=loaded.policy.content_hash,
        strategy_hash=hashlib.sha256(strategy.read_bytes()).hexdigest(),
        data_hash=hashlib.sha256(data.read_bytes()).hexdigest(),
    )
    trust, evidence = tmp_path / "trust.json", tmp_path / "evidence.json"
    trust.write_text(
        json.dumps(
            dict(
                schema_version=1,
                identity=asdict(identity),
                issuer_key_environment={"test-reviewer": "OWNER_TEST_KEY"},
                paper_account_id=account,
            )
        )
    )
    evidence.write_text(json.dumps(sign(dossier(identity))))
    from ops.worker_builder import WorkerPaths

    args = dict(
        trust_path=trust,
        evidence_path=evidence,
        checkout=Path(__file__).resolve().parents[2],
        strategy_path=strategy,
        data_path=data,
        environment=env,
        paths=WorkerPaths(
            tmp_path / "chosen-learning.json",
            tmp_path / "chosen-audit.jsonl",
            tmp_path / "reports",
            account,
        ),
        clock=lambda: now[0],
    )
    return args, journal, identity


def test_actual_builder_opens_existing_journal_without_activation_or_capture(construction):
    from infra.postgres import postgres_connection
    from ops.worker_builder import build_worker
    from portfolio.broker import AlpacaPaperBrokerAdapter

    args, journal, identity = construction
    before = journal.snapshot(identity.account_id, identity.mode)
    worker = build_worker(**args)
    try:
        assert worker.lease is None
        assert worker.runtime._agents == []
        assert worker.runtime._tick_count == 0
        assert worker.runtime.ingestion._quotes == {}
        assert type(worker.runtime.broker_adapter) is AlpacaPaperBrokerAdapter
        assert worker.runtime._performance_tracker.snapshot() == {}
        assert journal.snapshot(identity.account_id, identity.mode) == before
        with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM ah_control_workers WHERE account_id=%s",
                (identity.account_id,),
            )
            assert cur.fetchone()[0] == 0
    finally:
        worker.runtime.stop()


@pytest.mark.parametrize("change", ["account", "config", "artifact", "missing_account"])
def test_builder_fails_closed_before_activation(construction, change):
    from ops.worker_builder import build_worker

    args, _, _ = construction
    if change == "account":
        args["environment"]["PORTFOLIO_ACCOUNT_ID"] = "other"
    elif change == "config":
        args["environment"]["AGENT_TICK_INTERVAL"] = "17"
    elif change == "artifact":
        args["strategy_path"].write_bytes(args["strategy_path"].read_bytes() + b" ")
    else:
        # Matching explicit authority still cannot invent the journal's genesis.
        import json

        trust = json.loads(args["trust_path"].read_text())
        trust["identity"]["account_id"] = "uninitialized-account"
        args["trust_path"].write_text(json.dumps(trust))
        args["environment"]["PORTFOLIO_ACCOUNT_ID"] = "uninitialized-account"
    pattern = (
        "genesis"
        if change == "missing_account"
        else "artifacts" if change == "artifact" else "configuration"
    )
    with pytest.raises(Exception, match=pattern):
        build_worker(**args)
    assert not args["paths"].performance.exists()


def test_bus_missing_existing_schema_never_creates_tables(construction):
    from uuid import uuid4

    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from infra.postgres import postgres_connection

    _, journal, _ = construction
    schema = "builder_" + uuid4().hex
    with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(
        journal.dsn, options=f"-c search_path={schema} -c default_transaction_read_only=on"
    )
    with pytest.raises(Exception, match="ah_bus_events"):
        PostgresMessageBus(scoped, initialize_schema=False)
    with postgres_connection(journal.dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname=%s", (schema,))
        assert cur.fetchone()[0] == 0


def test_worker_cli_is_bounded_no_dotenv_or_implicit_start(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from cli.runtime import app

    paths = [
        tmp_path / name for name in ["trust.json", "evidence.json", "strategy.json", "data.json"]
    ]
    for path in paths:
        path.write_text("{}")
    flags = [
        "worker-run",
        "--trust-file",
        str(paths[0]),
        "--evidence-file",
        str(paths[1]),
        "--strategy-file",
        str(paths[2]),
        "--data-file",
        str(paths[3]),
        "--checkout",
        str(tmp_path),
        "--performance-file",
        str(tmp_path / "learning.json"),
        "--audit-file",
        str(tmp_path / "audit.jsonl"),
        "--report-directory",
        str(tmp_path / "reports"),
        "--instance-id",
        "test",
        "--max-iterations",
        "2",
    ]
    calls = []

    def build(**kwargs):
        calls.append("build")
        return SimpleNamespace(
            store=SimpleNamespace(account_id="a", mode="paper_broker"),
            run_once=lambda: calls.append("iteration"),
            runtime=SimpleNamespace(stop=lambda: calls.append("stop")),
        )

    monkeypatch.setattr("ops.worker_builder.build_worker", build)
    monkeypatch.setattr("cli.runtime.load_dotenv", lambda: pytest.fail("dotenv"))
    monkeypatch.setattr("cli.runtime.time.sleep", lambda seconds: calls.append("sleep"))
    result = CliRunner().invoke(app, flags)
    assert result.exit_code == 0, result.output
    assert calls == ["build", "iteration", "sleep", "iteration", "stop"]
    assert all(json.loads(line)["applied"] is False for line in result.output.splitlines())

    def bad(**kwargs):
        raise RuntimeError("private-connection-string")

    monkeypatch.setattr("ops.worker_builder.build_worker", bad)
    result = CliRunner().invoke(app, flags)
    assert result.exit_code == 2
    assert "private-connection-string" not in result.output
    assert "RuntimeError" in result.output


def test_paper_target_options_require_complete_independent_identity(tmp_path):
    from dataclasses import replace

    from ops.release_gate import ReleaseTrust
    from ops.worker_builder import paper_target_from_options
    from tests.ops.test_release_gate import IDENTITY, KEY

    trust = ReleaseTrust(
        replace(IDENTITY, mode="live", account_id="live-a"), {"owner": KEY}, "paper-a"
    )
    good = dict(
        account_id="paper-a",
        release="b" * 40,
        dsn_environment="EXISTING_PAPER_DSN",
        environment={"EXISTING_PAPER_DSN": "synthetic-private-dsn"},
        trust=trust,
    )
    target = paper_target_from_options(**good)
    assert target.store.account_id == "paper-a"
    assert target.store.mode == "paper_broker"
    assert target.release == "b" * 40
    for patch in [
        dict(account_id=None),
        dict(environment={}),
        dict(account_id="wrong"),
        dict(account_id="live-a"),
    ]:
        with pytest.raises(ValueError) as error:
            paper_target_from_options(**{**good, **patch})
        assert "synthetic-private-dsn" not in str(error.value)
    assert (
        paper_target_from_options(
            account_id=None, release=None, dsn_environment=None, environment={}, trust=trust
        )
        is None
    )


def test_cli_pairing_validates_before_builder_and_does_not_start_paper(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from cli.runtime import app
    from tests.ops.test_release_gate import KEY
    from tests.ops.test_worker_config import files

    trust, evidence = files.__wrapped__(tmp_path)
    monkeypatch.setenv("TEST_SIGNING_KEY", KEY.decode())
    monkeypatch.setenv("EXISTING_PAPER_DSN", "synthetic-private-paper-dsn")
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}")
    flags = [
        "worker-run",
        "--trust-file",
        str(trust),
        "--evidence-file",
        str(evidence),
        "--strategy-file",
        str(artifact),
        "--data-file",
        str(artifact),
        "--checkout",
        str(tmp_path),
        "--performance-file",
        str(tmp_path / "learning.json"),
        "--audit-file",
        str(tmp_path / "audit.jsonl"),
        "--report-directory",
        str(tmp_path / "reports"),
        "--instance-id",
        "test",
        "--max-iterations",
        "1",
    ]
    targets = []

    def build(**kwargs):
        targets.append(kwargs["paper_target"])
        return SimpleNamespace(
            store=SimpleNamespace(account_id="live", mode="live"),
            run_once=lambda: None,
            runtime=SimpleNamespace(stop=lambda: None),
        )

    monkeypatch.setattr("ops.worker_builder.build_worker", build)
    pair = [
        "--paper-account",
        "paper-owner",
        "--paper-release",
        "b" * 40,
        "--paper-dsn-environment",
        "EXISTING_PAPER_DSN",
    ]
    result = CliRunner().invoke(app, flags + pair)
    assert result.exit_code == 0, result.output
    assert len(targets) == 1 and targets[0].store.account_id == "paper-owner"
    for bad in [pair[:2], [*pair[:1], "wrong", *pair[2:]], [*pair[:-1], "MISSING_REFERENCE"]]:
        result = CliRunner().invoke(app, flags + bad)
        assert result.exit_code == 2
        assert "synthetic-private-paper-dsn" not in result.output
    assert len(targets) == 1
