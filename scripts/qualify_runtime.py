"""Run the isolated HTTP acceptance crash drill and write a sanitized evidence report."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

HTTP_DRILL = "test_process_kill_after_http_acceptance_recovers_without_repost"
RESTORE_DRILL = "test_process_kill_and_pg_restore_preserve_exact_journal_state"
DRILLS = {
    "http-acceptance": (
        HTTP_DRILL,
        "tests/integration/test_broker_crash_recovery.py::" + HTTP_DRILL,
        "http_acceptance_crash",
    ),
    "journal-restore": (
        RESTORE_DRILL,
        "tests/integration/test_journal_restore_drill.py::" + RESTORE_DRILL,
        "journal_transaction_and_restore",
    ),
}


@contextmanager
def exclusive_database(dsn: str) -> Iterator[None]:
    """Require a fresh database and hold a driver lease until its child finishes."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        locked = connection.execute(
            "SELECT pg_try_advisory_lock(hashtext('agenthedge-qualification-driver'))"
        ).fetchone()
        if locked != (True,):
            raise ValueError("qualification database already has an active driver")
        used = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid "
            "WHERE left(n.nspname,3)<>'pg_' AND n.nspname<>'information_schema')"
        ).fetchone()
        if used != (False,):
            raise ValueError("qualification database must be newly provisioned and empty")
        yield


def disposable_identity(dsn: str) -> dict[str, str]:
    from psycopg.conninfo import conninfo_to_dict

    try:
        identity = conninfo_to_dict(dsn) if dsn else {}
    except Exception:
        identity = {}
    if (
        identity.get("host") not in {"127.0.0.1", "localhost", "host.docker.internal"}
        or not identity.get("dbname", "").startswith("qualification_")
        or "hostaddr" in identity
        or "service" in identity
        or "servicefile" in identity
    ):
        raise ValueError("explicit disposable local qualification_* database required")
    return identity


@contextmanager
def exclusive_restore_databases(source: str, restored: str) -> Iterator[None]:
    source_identity = disposable_identity(source)
    restored_identity = disposable_identity(restored)
    if source_identity["dbname"] == restored_identity["dbname"]:
        raise ValueError("distinct disposable source and restore database names required")
    with exclusive_database(source), exclusive_database(restored):
        yield


def assess_junit(
    xml: str,
    *,
    returncode: int,
    expected_test: str = HTTP_DRILL,
    scope: str = "http_acceptance_crash",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "failed",
        "scope": scope,
        "platform_qualified": False,
        "process_returncode": returncode,
        "checks": [],
    }
    try:
        cases = ET.fromstring(xml).findall(".//testcase")
        if len(cases) != 1 or cases[0].get("name") != expected_test:
            return result
        case = cases[0]
        duration = float(case.get("time", "0"))
        if not math.isfinite(duration) or duration < 0:
            return result
        status = (
            "failed"
            if returncode or case.find("failure") is not None or case.find("error") is not None
            else "blocked" if case.find("skipped") is not None else "passed"
        )
        result.update(
            status=status,
            checks=[{"name": expected_test, "status": status, "seconds": duration}],
        )
    except (ET.ParseError, ValueError):
        pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--drill", choices=tuple(DRILLS), default="http-acceptance")
    parser.add_argument(
        "--disposable-database",
        action="store_true",
        help="Acknowledge the explicit local qualification database is disposable",
    )
    args = parser.parse_args()
    # Never load dotenv or print a DSN. This driver only accepts an explicitly named
    # disposable local database; fixtures create unique synthetic account namespaces.
    source_variable = (
        "E5B2_TEST_POSTGRES_DSN"
        if args.drill == "http-acceptance"
        else "O4_SOURCE_TEST_POSTGRES_DSN"
    )
    dsn = os.environ.get(source_variable, "")
    restore_dsn = os.environ.get("O4_RESTORE_TEST_POSTGRES_DSN", "")
    try:
        connection = disposable_identity(dsn)
        if args.drill == "journal-restore":
            restore_connection = disposable_identity(restore_dsn)
            if restore_connection["dbname"] == connection["dbname"]:
                raise ValueError("distinct disposable restore database required")
    except ValueError as exc:
        parser.error(str(exc))
    if not args.disposable_database:
        parser.error("--disposable-database acknowledgment required")
    if args.output.exists():
        parser.error("output already exists; choose a new evidence path")
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    overrides = {
        "EXECUTION_MODE": "simulated",
        "PYTHON_DOTENV_DISABLED": "1",
        "RUNTIME_PROFILE": "dev",
        "RUNTIME_BACKEND": "in_memory",
        "ALERT_WEBHOOK_URL": "",
    }
    environment = {**os.environ, **overrides}
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(root / "src"), str(root), os.environ.get("PYTHONPATH", ""))
    )
    started = datetime.now(timezone.utc)
    start_clock = time.monotonic()
    drill_name, test, scope = DRILLS[args.drill]
    with ExitStack() as stack:
        if args.drill == "http-acceptance":
            stack.enter_context(exclusive_database(dsn))
        # Restore takes both leases inside the maintained test so direct pytest
        # execution has the same final safety boundary as this driver.
        temporary = stack.enter_context(
            tempfile.TemporaryDirectory(prefix="agenthedge-acceptance-drill-")
        )
        base = Path(temporary)
        environment.update(
            PERFORMANCE_TRACKER_PATH=str(base / "performance.json"),
            AUDIT_LOG_PATH=str(base / "audit.jsonl"),
            LOG_DIR=str(base / "logs"),
        )
        xml_path = base / "result.xml"
        with (base / "pytest.log").open("w", encoding="utf-8") as log:
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    test,
                    "--no-cov",
                    "-q",
                    "--junitxml=" + str(xml_path),
                    "--basetemp=" + str(base / "tests"),
                ],
                cwd=root,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        report = assess_junit(
            xml_path.read_text(encoding="utf-8") if xml_path.exists() else "",
            returncode=process.returncode,
            expected_test=drill_name,
            scope=scope,
        )
    limitations = [
        "No real broker observations",
        "No host power-loss proof",
        "Other recovery, worker and stage gates require separate evidence",
    ]
    if args.drill == "http-acceptance":
        limitations.append("Database backup and restore require separate evidence")
    else:
        limitations.append("HTTP broker acceptance recovery requires separate evidence")
    report.update(
        schema_version=1,
        code_sha=revision,
        source_dirty=dirty,
        config_hash=hashlib.sha256(json.dumps(overrides, sort_keys=True).encode()).hexdigest(),
        started_at=started.isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=time.monotonic() - start_clock,
        system=platform.system(),
        synthetic_transport=("loopback_http" if args.drill == "http-acceptance" else "none"),
        database_scope=(
            "unique_synthetic_account"
            if args.drill == "http-acceptance"
            else "two_exclusive_disposable_databases"
        ),
        limitations=limitations,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, indent=2, allow_nan=False)
        output.write("\n")
    print(
        json.dumps({"status": report["status"], "source_dirty": dirty, "platform_qualified": False})
    )
    return 0 if report["status"] == "passed" and not dirty else 2


if __name__ == "__main__":
    raise SystemExit(main())
