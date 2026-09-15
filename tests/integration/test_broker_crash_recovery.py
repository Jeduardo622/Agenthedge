"""Kill a real child after loopback broker acceptance, then reconcile its journal."""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal as D
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from portfolio.activities import ActivityWindow
from portfolio.journal import EconomicEvent, PostgresJournal, TradePayload
from portfolio.reconciliation import (
    EconomicSnapshot,
    OrderWindow,
    ReconciledOrder,
    ReconciliationService,
)
from tests.integration.test_execution_durable_submission import Broker, agent, approval

pytest_plugins = ["tests.integration.test_execution_durable_submission"]


def test_qualification_driver_refuses_a_nonempty_database(bound):
    from scripts.qualify_runtime import exclusive_database

    with pytest.raises(ValueError, match="newly provisioned and empty"):
        with exclusive_database(bound[2]):
            pytest.fail("existing database must not reach drill execution")


def _child(account, port, directory):
    dsn = os.environ["E5B2_TEST_POSTGRES_DSN"]
    journal = PostgresJournal(dsn)
    journal._test_buses = []

    class AcceptedBroker(Broker):
        def submit_order(self, order):
            request = Request(
                f"http://127.0.0.1:{int(port)}/orders",
                data=json.dumps({"client_order_id": order.client_order_id}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=10) as response:
                assert response.status == 200
                assert json.load(response)["accepted"] is True
            # Parent kills here: HTTP accepted, no local observation/acknowledgment.
            (Path(directory) / "accepted-before-observation").write_text("ready", encoding="utf-8")
            threading.Event().wait(60)
            raise AssertionError("parent did not terminate child at the acceptance boundary")

    broker = AcceptedBroker(account)
    execution = agent((journal, account, dsn), broker, Path(directory))
    execution._handle_approval(approval(broker=broker))
    raise AssertionError("expected child to block after broker acceptance")


def test_process_kill_after_http_acceptance_recovers_without_repost(bound, tmp_path):
    journal, account, dsn = bound
    accepted = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/orders"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            accepted.append((payload["client_order_id"], datetime.now(timezone.utc)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"accepted":true}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    child = None
    try:
        with (tmp_path / "child.log").open("w", encoding="utf-8") as output:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tests.integration.test_broker_crash_recovery",
                    "--child",
                    account,
                    str(server.server_port),
                    str(tmp_path),
                ],
                env={
                    **os.environ,
                    "PYTHON_DOTENV_DISABLED": "1",
                    "EXECUTION_MODE": "simulated",
                    "E5B2_TEST_POSTGRES_DSN": dsn,
                },
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            deadline = time.monotonic() + 25
            while not (tmp_path / "accepted-before-observation").exists():
                assert child.poll() is None, "child exited before the acceptance boundary"
                assert time.monotonic() < deadline, "child did not reach the acceptance boundary"
                time.sleep(0.05)
            assert [item[0] for item in accepted] == ["approval"]
            assert journal.intent(account, "paper_broker", "approval")["status"] == "unknown"
            assert journal.snapshot(account, "paper_broker").cash == D(1000)
            child.kill()
            child.wait(timeout=10)
            assert child.returncode != 0

        class RecoveryBroker(Broker):
            filled = False

            def get_reconciliation_order(self, client, **kwargs):
                assert client == "approval"
                return ReconciledOrder(
                    "broker",
                    "approval",
                    "SPY",
                    D(2),
                    "buy",
                    "filled" if self.filled else "accepted",
                    D(2) if self.filled else D(0),
                    D(100) if self.filled else D(0),
                    accepted[0][1],
                    {},
                )

            def get_order_window(self, **kwargs):
                order = self.get_reconciliation_order("approval")
                orders = () if kwargs.get("scope") == "open" and self.filled else (order,)
                return OrderWindow(account, "paper_broker", orders, True, (), self.now())

            def get_economic_snapshot(self, **kwargs):
                return EconomicSnapshot(
                    account,
                    "paper_broker",
                    D(800) if self.filled else D(1000),
                    {"SPY": D(2)} if self.filled else {},
                    self.now(),
                )

            def get_activity_window(self, **kwargs):
                event = EconomicEvent(
                    account,
                    "paper_broker",
                    "observed-fill",
                    accepted[0][1],
                    "synthetic-fill-source",
                    TradePayload("broker", "SPY", D(2), D(100), D(0)),
                )
                return ActivityWindow(
                    account,
                    "paper_broker",
                    kwargs["after"],
                    kwargs["until"],
                    self.now(),
                    (),
                    (event,) if self.filled else (),
                    True,
                    (),
                )

            def submit_order(self, order):
                raise AssertionError("recovery must never repost an accepted order")

        broker = RecoveryBroker(account)
        reconciler = ReconciliationService(PostgresJournal(dsn), broker)
        assert reconciler.reconcile(account, "paper_broker").complete
        assert journal.intent(account, "paper_broker", "approval")["status"] == "observed"
        assert journal.reservations(account, "paper_broker")[0].remaining_quantity == D(2)
        broker.filled = True
        assert reconciler.reconcile(account, "paper_broker").complete
        assert reconciler.reconcile(account, "paper_broker").complete
        assert journal.snapshot(account, "paper_broker").cash == D(800)
        assert journal.snapshot(account, "paper_broker").positions["SPY"].quantity == D(2)
        assert journal.checkpoint(account, "paper_broker") == 1
        assert journal.reservations(account, "paper_broker") == ()
        assert not journal.claim_intent_submission(account, "paper_broker", "approval")
        assert len(accepted) == 1
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        server.shutdown()
        server.server_close()
        serving.join(timeout=5)


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--child":
        raise SystemExit("test child entry only")
    _child(sys.argv[2], sys.argv[3], sys.argv[4])
