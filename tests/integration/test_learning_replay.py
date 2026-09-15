"""Read-only attribution replay from actual disposable journal ownership."""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import pytest

from infra.postgres import migrate_execution_journal
from learning.performance import PerformanceTracker
from learning.replay import export_journal_attribution
from portfolio.accounting import AccountingState, PositionState
from portfolio.journal import (
    CorrectionPayload,
    EconomicEvent,
    OrderObservation,
    PostgresJournal,
    TradePayload,
)
from risk.valuation import WorkingOrderReservation

NOW = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)


@pytest.fixture
def journal():
    dsn = os.environ.get("PROJECTION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("disposable PROJECTION_TEST_POSTGRES_DSN required")
    migrate_execution_journal(dsn, apply=True, target_version=2)
    journal = PostgresJournal(dsn)
    account = "learning-replay-" + uuid4().hex
    journal.initialize_account(account, "simulated", AccountingState(D(1000), D(0), {}))
    return journal, account


def trade(journal, account, client, quantity, price, owners, second):
    side = "buy" if quantity > 0 else "sell"
    journal.record_intent(
        account,
        "simulated",
        client,
        {"strategies": owners},
        reservation=WorkingOrderReservation(
            client,
            "SPY",
            side,
            abs(quantity),
            price,
            abs(quantity) * price if quantity > 0 else D(0),
            "submitted",
        ),
    )
    journal.observe_order(
        account,
        "simulated",
        client,
        OrderObservation(client, client, "SPY", side, abs(quantity), D(0), D(0), "accepted"),
    )
    journal.apply_order_event(
        EconomicEvent(
            account,
            "simulated",
            client,
            NOW + timedelta(seconds=second),
            "synthetic",
            TradePayload(client, "SPY", quantity, price, D(0)),
        ),
        client_order_id=client,
    )


def test_replay_uses_original_entry_owners_across_reopen_and_correction(journal, tmp_path):
    j, account = journal
    owners = [
        {"strategy": "value", "confidence": 0.75},
        {"strategy": "momentum", "confidence": 0.25},
    ]
    trade(j, account, "entry", D(2), D(100), owners, 0)
    trade(j, account, "exit", D(-1), D(120), [{"strategy": "exit", "confidence": 1}], 1)
    j.apply_order_event(
        EconomicEvent(
            account,
            "simulated",
            "correct-entry",
            NOW + timedelta(seconds=2),
            "synthetic",
            CorrectionPayload("entry", TradePayload("entry", "SPY", D(2), D(90), D(0))),
        ),
        client_order_id="entry",
    )
    before = (
        j.snapshot(account, "simulated"),
        j.checkpoint(account, "simulated"),
        j.outbox(account, "simulated"),
    )
    report = export_journal_attribution(j, account_id=account, mode="simulated")
    assert {key: D(value) for key, value in report["realized_pnl"].items()} == {
        "value": D("22.50"),
        "momentum": D("7.50"),
    }
    assert report["unavailable_event_ids"] == []
    assert report["checkpoint"] == 3
    rebuilt = PerformanceTracker(tmp_path / "rebuilt.json")
    for envelope in report["envelopes"]:
        rebuilt.record_economic_event(envelope)
    assert rebuilt.snapshot()["value"]["realized_pnl"] == 22.5
    assert rebuilt.weights()["value"] == 1.0
    assert (
        export_journal_attribution(PostgresJournal(j.dsn), account_id=account, mode="simulated")
        == report
    )
    assert (
        j.snapshot(account, "simulated"),
        j.checkpoint(account, "simulated"),
        j.outbox(account, "simulated"),
    ) == before


def test_export_keeps_identical_order_ids_in_other_namespace_separate(journal):
    j, account = journal
    other = account + "-other"
    j.initialize_account(other, "simulated", AccountingState(D(1000), D(0), {}))
    for namespace, owner in ((account, "value"), (other, "other")):
        trade(j, namespace, "entry", D(1), D(100), [{"strategy": owner, "confidence": 1}], 0)
        trade(j, namespace, "exit", D(-1), D(110), [], 1)
    report = export_journal_attribution(j, account_id=account, mode="simulated")
    assert report["realized_pnl"] == {"value": "10"}
    assert all(item["economic_event"]["account_id"] == account for item in report["envelopes"])
    with pytest.raises(ValueError, match="namespace"):
        export_journal_attribution(j, account_id=account, mode="live")


def test_unowned_economics_remain_explicitly_unavailable(journal):
    j, account = journal
    j.apply_event(
        EconomicEvent(
            account,
            "simulated",
            "external",
            NOW,
            "synthetic",
            TradePayload("external-order", "SPY", D(1), D(100), D(0)),
        )
    )
    report = export_journal_attribution(j, account_id=account, mode="simulated")
    assert report["unavailable_event_ids"] == ["external"]
    assert report["realized_pnl"] == {}


def test_preexisting_positions_without_entry_history_cannot_claim_complete_replay(journal):
    j, account = journal
    account += "-preexisting"
    j.initialize_account(
        account, "simulated", AccountingState(D(1000), D(0), {"SPY": PositionState(D(2), D(90))})
    )
    with pytest.raises(ValueError, match="initial position ownership"):
        export_journal_attribution(j, account_id=account, mode="simulated")


def test_cli_creates_report_and_refuses_to_overwrite(journal, tmp_path):
    j, account = journal
    output = tmp_path / "attribution.json"
    args = [
        sys.executable,
        "scripts/replay_learning.py",
        "--dsn-env",
        "PROJECTION_TEST_POSTGRES_DSN",
        "--account",
        account,
        "--mode",
        "simulated",
        "--output",
        str(output),
    ]
    options = dict(
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    first = subprocess.run(args, **options)
    assert first.returncode == 0, first.stderr
    saved = output.read_bytes()
    assert json.loads(saved)["account_id"] == account
    assert json.loads(saved)["checkpoint"] == 0
    second = subprocess.run(args, **options)
    assert second.returncode == 1
    assert "FileExistsError" in second.stderr
    assert j.dsn not in first.stdout + first.stderr + second.stdout + second.stderr
    assert output.read_bytes() == saved
