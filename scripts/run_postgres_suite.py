"""Full CI suite on newly created local PostgreSQL databases; never adopt or drop data."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

PG_VARIABLES = (
    "POSTGRES_DSN",
    "E3_TEST_POSTGRES_DSN",
    "PROJECTION_TEST_POSTGRES_DSN",
    "E5B2_TEST_POSTGRES_DSN",
    "E5_TEST_POSTGRES_DSN",
    "E6_TEST_POSTGRES_DSN",
    "E4_TEST_POSTGRES_DSN",
    "E4B3_TEST_POSTGRES_DSN",
    "O1_TEST_POSTGRES_DSN",
    "E4B1_TEST_POSTGRES_DSN",
    "CLOSEOUT_VIEW_TEST_POSTGRES_DSN",
    "NONTRADE_TEST_POSTGRES_DSN",
    "R1B3_TEST_POSTGRES_DSN",
    "E6C_TEST_POSTGRES_DSN",
    "RUNTIME_RELEASE_TEST_POSTGRES_DSN",
    "R1_RUNTIME_TEST_POSTGRES_DSN",
    "R5B_TEST_POSTGRES_DSN",
    "WORKER_BUILDER_TEST_POSTGRES_DSN",
    "R2_TEST_POSTGRES_DSN",
    "O1_REARM_TEST_POSTGRES_DSN",
    "O4_SOURCE_TEST_POSTGRES_DSN",
    "O4_RESTORE_TEST_POSTGRES_DSN",
)


class SetupGuardError(ValueError):
    """A deliberately safe, actionable setup diagnostic without connection values."""


def setup_error(error: Exception) -> str:
    detail = str(error) if isinstance(error, SetupGuardError) else type(error).__name__
    return "PostgreSQL suite setup failed: " + detail


@dataclass(frozen=True)
class DatabaseTarget:
    variable: str
    name: str
    dsn: str = field(repr=False)


def child_environment(
    inherited: dict[str, str], targets: tuple[DatabaseTarget, ...], root: Path
) -> dict[str, str]:
    environment = dict(inherited)
    environment.pop("PYTEST_ADDOPTS", None)
    environment.update({target.variable: target.dsn for target in targets})
    environment.update(
        PYTHON_DOTENV_DISABLED="1",
        EXECUTION_MODE="simulated",
        RUNTIME_PROFILE="dev",
        RUNTIME_BACKEND="in_memory",
        PYTHONPATH=os.pathsep.join((str(root / "src"), str(root))),
    )
    return environment


def database_plan(admin_dsn: str, run_id: str) -> tuple[DatabaseTarget, ...]:
    try:
        connection = conninfo_to_dict(admin_dsn)
    except Exception:
        raise SetupGuardError("invalid explicit local PostgreSQL connection") from None
    if (
        connection.get("host") not in {"localhost", "127.0.0.1"}
        or connection.get("dbname") != "postgres"
        or not connection.get("user")
        or set(connection) - {"host", "port", "dbname", "user", "password"}
        or re.fullmatch(r"[a-f0-9]{32}", run_id) is None
    ):
        raise SetupGuardError("explicit loopback postgres administration database required")
    return tuple(
        DatabaseTarget(variable, name, make_conninfo(**{**connection, "dbname": name}))
        for index, variable in enumerate(PG_VARIABLES)
        for name in [f"qualification_{run_id}_{index:02d}"]
    )


def verify_environment_map(tests: Path) -> None:
    referenced = set()
    for path in tests.rglob("*.py"):
        referenced.update(re.findall(r"\b[A-Z][A-Z0-9_]*_TEST_POSTGRES_DSN\b", path.read_text()))
    if referenced - set(PG_VARIABLES):
        raise SetupGuardError(
            "unmapped PostgreSQL fixture variables: "
            + ",".join(sorted(referenced - set(PG_VARIABLES)))
        )


def create_databases(admin_dsn: str, targets: tuple[DatabaseTarget, ...]) -> None:
    if not targets or targets != database_plan(
        admin_dsn, targets[0].name.removeprefix("qualification_").rsplit("_", 1)[0]
    ):
        raise SetupGuardError("complete generated disposable database plan required")
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        if connection.info.server_version // 10000 != 16:
            raise SetupGuardError("PostgreSQL 16 server required")
        used = connection.execute(
            "SELECT datname FROM pg_database WHERE datname=ANY(%s)",
            ([target.name for target in targets],),
        ).fetchall()
        if used:
            raise SetupGuardError("disposable database already exists; no adoption permitted")
        for target in targets:
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(target.name))
            )


def assess_results(xml: str, manifest: dict, returncode: int) -> dict:
    result = {
        "passed": False,
        "executed": 0,
        "collected": manifest.get("collected"),
        "returncode": returncode,
    }
    try:
        cases = ET.fromstring(xml).findall(".//testcase")
        result["executed"] = len(cases)
        result["passed"] = bool(
            returncode == 0
            and cases
            and type(manifest.get("collected")) is int
            and len(cases) == manifest["collected"]
            and not manifest.get("deselected", 0)
            and not manifest.get("collection_skipped", 0)
            and all(
                not any(case.find(tag) is not None for tag in ("skipped", "failure", "error"))
                for case in cases
            )
        )
    except (ET.ParseError, TypeError, ValueError):
        pass
    return result


class CollectionManifest:
    def __init__(self, path: Path):
        self.path = path
        self.deselected = 0
        self.collection_skipped = 0

    def pytest_deselected(self, items):
        self.deselected += len(items)

    def pytest_collectreport(self, report):
        if report.skipped:
            self.collection_skipped += 1

    def pytest_sessionfinish(self, session, exitstatus):
        self.path.write_text(
            json.dumps(
                {
                    "collected": session.testscollected,
                    "deselected": self.deselected,
                    "collection_skipped": self.collection_skipped,
                }
            )
        )


def pytest_addoption(parser):
    parser.addoption("--pg-suite-manifest", default=None)


def pytest_configure(config):
    path = config.getoption("--pg-suite-manifest")
    if path:
        config.pluginmanager.register(CollectionManifest(Path(path)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--create-disposable-databases", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        if not args.create_disposable_databases:
            raise SetupGuardError("explicit disposable database creation acknowledgment required")
        verify_environment_map(root / "tests")
        admin = os.environ.get("POSTGRES_DSN", "")
        targets = database_plan(admin, uuid.uuid4().hex)
        output = args.output_dir.resolve()
        if not output.is_relative_to((root / ".cache").resolve()):
            raise SetupGuardError("evidence directory must be inside repository .cache")
        if output.exists():
            raise SetupGuardError("new evidence directory required")
        if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip():
            raise SetupGuardError("clean committed source required")
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        for name in ("pg_dump", "pg_restore"):
            tool = shutil.which(name)
            if not tool or not re.search(
                r"\b16\.\d+", subprocess.check_output([tool, "--version"], text=True)
            ):
                raise SetupGuardError("PostgreSQL 16 dump and restore tools required")
        output.mkdir(parents=True)
        create_databases(admin, targets)
        environment = child_environment(dict(os.environ), targets, root)
        junit, collection = output / "junit.xml", output / "collection.json"
        started = time.monotonic()
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests",
                "--cov=src",
                "--cov-fail-under=80",
                "--cov-report=term",
                "-p",
                "scripts.run_postgres_suite",
                "--pg-suite-manifest",
                str(collection),
                "--junitxml",
                str(junit),
            ],
            cwd=root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        log = process.stdout
        for dsn in (admin, *(target.dsn for target in targets)):
            log = log.replace(dsn, "[redacted PostgreSQL connection]")
        (output / "pytest.txt").write_text(log)
        print(log, end="")
        manifest = json.loads(collection.read_text()) if collection.exists() else {}
        xml = junit.read_text() if junit.exists() else ""
        for dsn in (admin, *(target.dsn for target in targets)):
            xml = xml.replace(dsn, "[redacted PostgreSQL connection]")
        if junit.exists():
            junit.write_text(xml)
        result = assess_results(xml, manifest, process.returncode)
        result.update(
            sha=revision,
            seconds=time.monotonic() - started,
            coverage_threshold=80,
            databases={target.variable: target.name for target in targets},
        )
        (output / "result.json").write_text(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1
    except Exception as exc:
        print(setup_error(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
