import json
from datetime import timedelta

import pytest
from click import unstyle
from typer import rich_utils
from typer.testing import CliRunner

from cli.backtest import app
from cli.promotion_gate import app as gate
from tests.backtest.test_validation_adapter import qualified_bundle


def test_cli_actual_qualification_and_promotion_rejects_insufficient(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    _, sessions = qualified_bundle(tmp_path, signal=False)
    plan = {
        "objective": "net_return",
        "train": [sessions[0][1].isoformat(), sessions[62][1].isoformat()],
        "validation": [sessions[62][1].isoformat(), sessions[65][1].isoformat()],
        "holdout": [
            sessions[65][1].isoformat(),
            (sessions[-1][1] + timedelta(seconds=1)).isoformat(),
        ],
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    result = CliRunner().invoke(
        app,
        [
            "--symbol",
            "SPY",
            "--start",
            str(sessions[0][0]),
            "--end",
            str(sessions[-1][0]),
            "--dataset-bundle",
            str(tmp_path / "bundle.json"),
            "--storage-dir",
            str(tmp_path / "runs"),
            "--validation-protocol",
            str(path),
        ],
    )
    assert result.exit_code == 0, result.exception
    assert "insufficient_evidence" in result.output
    artifact = tmp_path / "runs" / "qualification.json"
    import hashlib

    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    rejection = CliRunner().invoke(
        gate, ["--qualification-artifact", str(artifact), "--qualification-sha256", digest]
    )
    assert rejection.exit_code == 1, rejection.exception
    assert "insufficient_evidence" in rejection.output


@pytest.mark.parametrize("force_color", [False, True], ids=["plain", "forced-color"])
def test_validation_route_requires_qualified_bundle(tmp_path, monkeypatch, force_color):
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", force_color)
    monkeypatch.setattr(rich_utils, "COLOR_SYSTEM", "standard" if force_color else None)
    result = CliRunner().invoke(
        app,
        [
            "--symbol",
            "SPY",
            "--start",
            "2024-01-01",
            "--end",
            "2024-02-01",
            "--validation-protocol",
            str(tmp_path / "plan.json"),
        ],
        color=True,
    )
    assert result.exit_code == 2
    assert ("\x1b[" in result.output) is force_color
    assert "--validation-protocol requires --dataset-bundle" in unstyle(result.output)
