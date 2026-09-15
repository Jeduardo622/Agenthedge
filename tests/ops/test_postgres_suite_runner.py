"""Fail-closed full PostgreSQL suite orchestration, without live database access."""

import pytest
from scripts.run_postgres_suite import (
    PG_VARIABLES,
    assess_results,
    database_plan,
    verify_environment_map,
)


def test_distinct_disposable_databases_preserve_only_explicit_local_connection():
    targets = database_plan(
        "postgresql://synthetic:private-value@127.0.0.1:55443/postgres", "a" * 32
    )
    assert {item.variable for item in targets} == set(PG_VARIABLES)
    assert len({item.name for item in targets}) == len(PG_VARIABLES)
    assert all(item.name.startswith("qualification_") for item in targets)
    assert "private-value" not in repr(targets)
    assert len({item.dsn for item in targets}) == len(PG_VARIABLES)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://user:secret@external.invalid/postgres",
        "postgresql://user:secret@localhost/existing_account",
        "host=localhost hostaddr=192.0.2.1 dbname=postgres user=user",
        "service=existing",
    ],
)
def test_unqualified_database_target_rejected(dsn):
    with pytest.raises(ValueError):
        database_plan(dsn, "a" * 32)


def test_test_environment_map_cannot_silently_omit_new_fixture(tmp_path):
    (tmp_path / "test_future.py").write_text('env.get("FUTURE' + '_TEST_POSTGRES_DSN")')
    with pytest.raises(ValueError, match="unmapped"):
        verify_environment_map(tmp_path)


def test_actual_repository_environment_map_is_complete():
    from pathlib import Path

    verify_environment_map(Path(__file__).resolve().parents[1])


@pytest.mark.parametrize(
    "case,manifest,code",
    [
        ("<testcase><skipped/></testcase>", {"collected": 1}, 0),
        ("<testcase><failure/></testcase>", {"collected": 1}, 0),
        ("<testcase/>", {"collected": 2}, 0),
        ("", {"collected": 0}, 0),
        ("<testcase/>", {"collected": 1, "deselected": 1}, 0),
        ("<testcase/>", {"collected": 1, "collection_skipped": 1}, 0),
        ("<testcase/>", {"collected": 1}, 1),
    ],
)
def test_partial_skipped_or_failed_run_never_passes(case, manifest, code):
    assert not assess_results("<testsuite>" + case + "</testsuite>", manifest, code)["passed"]


def test_complete_run_passes():
    result = assess_results("<testsuite><testcase/></testsuite>", {"collected": 1}, 0)
    assert result["passed"] and result["executed"] == 1


def test_operator_environment_cannot_change_suite_runtime_contract(tmp_path):
    from scripts.run_postgres_suite import child_environment

    targets = database_plan("postgresql://synthetic:private@localhost/postgres", "b" * 32)
    inherited = {
        "RUNTIME_PROFILE": "staging",
        "RUNTIME_BACKEND": "postgres",
        "EXECUTION_MODE": "live",
        "PYTEST_ADDOPTS": "-k omit",
    }
    child = child_environment(inherited, targets, tmp_path)
    assert child["RUNTIME_PROFILE"] == "dev"
    assert child["RUNTIME_BACKEND"] == "in_memory"
    assert child["EXECUTION_MODE"] == "simulated"
    assert child["PYTHON_DOTENV_DISABLED"] == "1"
    assert "PYTEST_ADDOPTS" not in child
    assert inherited["EXECUTION_MODE"] == "live"


def test_setup_error_only_exposes_deliberately_safe_guard_messages():
    from scripts.run_postgres_suite import SetupGuardError, setup_error

    assert "clean committed source required" in setup_error(
        SetupGuardError("clean committed source required")
    )
    assert "private-password" not in setup_error(ValueError("DSN private-password"))


def test_existing_database_collision_performs_no_create(monkeypatch):
    from types import SimpleNamespace

    from scripts.run_postgres_suite import create_databases

    admin = "postgresql://synthetic:private-value@localhost/postgres"
    targets = database_plan(admin, "a" * 32)
    statements = []

    class Connection:
        info = SimpleNamespace(server_version=160013)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params=None):
            statements.append(str(query))
            return SimpleNamespace(fetchall=lambda: [(targets[0].name,)])

    monkeypatch.setattr("scripts.run_postgres_suite.psycopg.connect", lambda *a, **kw: Connection())
    with pytest.raises(ValueError, match="already exists"):
        create_databases(admin, targets)
    assert len(statements) == 1 and statements[0].startswith("SELECT")


@pytest.mark.parametrize(
    "extra,body,expected",
    [
        ([], "def test_ok(): pass", True),
        ([], "import pytest\n@pytest.mark.skip(reason='synthetic')\ndef test_ok(): pass", False),
        (["-k", "test_ok"], "def test_ok(): pass\ndef test_unselected(): pass", False),
    ],
)
def test_real_pytest_plugin_accounts_for_all_tests(tmp_path, extra, body, expected):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    (tmp_path / "test_probe.py").write_text(body)
    manifest, junit = tmp_path / "collection.json", tmp_path / "junit.xml"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path / "test_probe.py"),
            "-q",
            "-p",
            "scripts.run_postgres_suite",
            "--pg-suite-manifest",
            str(manifest),
            "--junitxml",
            str(junit),
            *extra,
        ],
        env={**os.environ, "PYTHONPATH": str(root), "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
    )
    assert manifest.exists(), process.stdout + process.stderr
    result = assess_results(junit.read_text(), json.loads(manifest.read_text()), process.returncode)
    assert result["passed"] is expected
