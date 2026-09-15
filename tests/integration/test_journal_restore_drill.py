"""Disposable process-termination and PostgreSQL restore acceptance drill."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from infra.postgres import ensure_postgres_schema, migrate_execution_journal, postgres_connection
from ops.commands import migrate_control_commands
from portfolio.accounting import AccountingState
from portfolio.journal import EconomicEvent, OrderObservation, PostgresJournal, TradePayload
from risk.valuation import WorkingOrderReservation

ACCOUNT_PREFIX = "qualification-restore"


def _trade(account: str) -> EconomicEvent:
    return EconomicEvent(
        account,
        "simulated",
        "fill-1",
        datetime(2026, 1, 2, 20, tzinfo=timezone.utc),
        "synthetic-restore-fixture-v1",
        TradePayload(
            "broker-order-1",
            "SPY",
            Decimal("1"),
            Decimal("100"),
            Decimal("1.25"),
            "synthetic-fee-1",
        ),
    )


def _child(dsn: str, account: str, marker: str, phase: str) -> None:
    import portfolio.journal as journal_module

    original = journal_module.postgres_connection

    def pause() -> None:
        Path(marker).write_text(phase, encoding="utf-8")
        while True:
            time.sleep(0.02)

    @contextmanager
    def boundary(target: str):
        with original(target) as connection:
            yield connection
            if phase == "before_commit":
                pause()
        if phase == "after_commit":
            pause()

    journal_module.postgres_connection = boundary
    PostgresJournal(dsn).apply_order_event(_trade(account), client_order_id="client-1")


def _wait_and_kill(process: subprocess.Popen, marker: Path) -> None:
    deadline = time.monotonic() + 20
    while not marker.exists():
        if process.poll() is not None:
            raise RuntimeError("child exited before the transaction boundary")
        if time.monotonic() > deadline:
            raise RuntimeError("child did not reach the transaction boundary")
        time.sleep(0.02)
    process.kill()
    process.wait(timeout=10)


def _postgres_tool(executable: str, *args: str) -> None:
    subprocess.run(
        [executable, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _account_state(journal: PostgresJournal, account: str) -> dict[str, object]:
    return {
        "snapshot": journal.snapshot(account, "simulated"),
        "snapshot_with_timestamp": journal.snapshot_with_timestamp(account, "simulated"),
        "checkpoint": journal.checkpoint(account, "simulated"),
        "orders": journal.list_order_states(account, "simulated"),
        "reservations": journal.reservations(account, "simulated"),
        "outbox": journal.outbox(account, "simulated"),
    }


def test_process_kill_and_pg_restore_preserve_exact_journal_state(tmp_path: Path) -> None:
    from scripts.qualify_runtime import exclusive_restore_databases

    source_dsn = os.environ.get("O4_SOURCE_TEST_POSTGRES_DSN", "")
    restore_dsn = os.environ.get("O4_RESTORE_TEST_POSTGRES_DSN", "")
    if not source_dsn or not restore_dsn:
        pytest.skip("dedicated disposable O4 source and restore databases required")
    dump_tool, restore_tool = shutil.which("pg_dump"), shutil.which("pg_restore")
    assert dump_tool and restore_tool, "PostgreSQL pg_dump and pg_restore required on PATH"
    with exclusive_restore_databases(source_dsn, restore_dsn):
        _run_restore_drill(tmp_path, source_dsn, restore_dsn, dump_tool, restore_tool)


def _run_restore_drill(
    tmp_path: Path, source_dsn: str, restore_dsn: str, dump_tool: str, restore_tool: str
) -> None:
    ensure_postgres_schema(source_dsn)
    migrate_execution_journal(source_dsn, apply=True, target_version=6)
    migrate_control_commands(source_dsn, apply=True)
    journal = PostgresJournal(source_dsn)

    for phase in ("before_commit", "after_commit"):
        account = f"{ACCOUNT_PREFIX}-{phase}"
        journal.initialize_account(
            account, "simulated", AccountingState(Decimal("1000"), Decimal("0"), {})
        )
        journal.record_intent(
            account,
            "simulated",
            "client-1",
            {"source": "synthetic-restore-drill"},
            reservation=WorkingOrderReservation(
                "client-1",
                "SPY",
                "buy",
                Decimal("2"),
                Decimal("120"),
                Decimal("240"),
                "submitted",
            ),
        )
        journal.observe_order(
            account,
            "simulated",
            "client-1",
            OrderObservation(
                "broker-order-1",
                "client-1",
                "SPY",
                "buy",
                Decimal("2"),
                Decimal("0"),
                Decimal("0"),
                "accepted",
            ),
        )
        marker = tmp_path / phase
        process = subprocess.Popen(
            [sys.executable, __file__, "child", source_dsn, account, str(marker), phase],
            env=os.environ.copy(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            _wait_and_kill(process, marker)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        committed = phase == "after_commit"
        assert journal.snapshot(account, "simulated").cash == (
            Decimal("898.75") if committed else Decimal("1000")
        )
        assert journal.checkpoint(account, "simulated") == int(committed)
        assert len(journal.outbox(account, "simulated")) == int(committed)
        assert journal.apply_order_event(_trade(account), client_order_id="client-1") is (
            not committed
        )
        state = journal.snapshot(account, "simulated")
        assert state.cash == Decimal("898.75")
        assert state.positions["SPY"].quantity == Decimal("1")
        assert state.positions["SPY"].average_cost == Decimal("100")
        order = journal.order_state(account, "simulated", "client-1")
        assert Decimal(order["posted_quantity"]) == Decimal("1")
        assert Decimal(order["posted_value"]) == Decimal("100")
        assert Decimal(order["posted_fee"]) == Decimal("1.25")
        assert Decimal(order["remaining_quantity"]) == Decimal("1")
        assert journal.reservations(account, "simulated")[0].remaining_quantity == Decimal("1")

    dump = str(tmp_path / "journal.dump")
    _postgres_tool(dump_tool, "--dbname", source_dsn, "-Fc", "-f", dump)
    _postgres_tool(restore_tool, "--dbname", restore_dsn, "--exit-on-error", dump)

    restored = PostgresJournal(restore_dsn)
    for phase in ("before_commit", "after_commit"):
        account = f"{ACCOUNT_PREFIX}-{phase}"
        assert _account_state(restored, account) == _account_state(journal, account)
        assert restored.apply_order_event(_trade(account), client_order_id="client-1") is False
        assert _account_state(restored, account) == _account_state(journal, account)
    with postgres_connection(restore_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version FROM ah_execution_schema")
        assert cursor.fetchone() == (6,)
        cursor.execute("SELECT version FROM ah_control_schema")
        assert cursor.fetchone() == (1,)


if __name__ == "__main__" and len(sys.argv) == 6 and sys.argv[1] == "child":
    _child(*sys.argv[2:])
