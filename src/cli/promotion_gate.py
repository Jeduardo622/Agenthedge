"""CLI for evaluating backtest promotion reports against explicit thresholds."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import typer

app = typer.Typer(help="Evaluate promotion_report.json artifacts")


@app.command()
def main(
    report: str = typer.Option(..., "--report", help="Path to promotion_report.json"),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="JSON threshold profile to apply before CLI overrides",
    ),
    min_trades: int | None = typer.Option(None, "--min-trades", help="Minimum total trades"),
    min_catalyst_trades: int | None = typer.Option(
        None,
        "--min-catalyst-trades",
        help="Minimum catalyst-attributed trades",
    ),
    min_return_pct: float | None = typer.Option(
        None,
        "--min-return-pct",
        help="Minimum return_pct from the promotion report",
    ),
    required_promotion_status: str | None = typer.Option(
        None,
        "--required-promotion-status",
        help="Required catalyst.promotion_status value",
    ),
    require_fixture_backed: bool = typer.Option(
        False,
        "--require-fixture-backed",
        help="Require validation.fixture_backed to be true",
    ),
    require_no_live_network: bool = typer.Option(
        False,
        "--require-no-live-network",
        help="Require validation.no_live_network to be true",
    ),
    require_catalyst_opt_in: bool = typer.Option(
        False,
        "--require-catalyst-opt-in",
        help="Require validation.catalyst_opt_in to be true",
    ),
    require_packet_loaded: bool = typer.Option(
        False,
        "--require-packet-loaded",
        help="Require validation.packet_loaded to be true",
    ),
    require_no_stale_catalyst_trades: bool = typer.Option(
        False,
        "--require-no-stale-catalyst-trades",
        help="Require validation.no_stale_catalyst_trades to be true",
    ),
) -> None:
    """Read a promotion report and fail if any explicit gate condition is unmet."""

    payload = _load_report(report)
    profile_config = _load_profile(profile) if profile else {}
    failures = evaluate_with_profile(
        payload,
        profile_config,
        min_trades=min_trades,
        min_catalyst_trades=min_catalyst_trades,
        min_return_pct=min_return_pct,
        required_promotion_status=required_promotion_status,
        required_validation_flags={
            "fixture_backed": require_fixture_backed,
            "no_live_network": require_no_live_network,
            "catalyst_opt_in": require_catalyst_opt_in,
            "packet_loaded": require_packet_loaded,
            "no_stale_catalyst_trades": require_no_stale_catalyst_trades,
        },
    )
    run_id = str(payload.get("run_id", "<unknown>"))
    if failures:
        typer.echo(f"PROMOTION_GATE_FAIL {run_id}")
        for failure in failures:
            typer.echo(f"- {failure}")
        raise typer.Exit(1)
    typer.echo(f"PROMOTION_GATE_PASS {run_id}")


def evaluate_promotion_report(
    report: Mapping[str, Any],
    *,
    min_trades: int | None = None,
    min_catalyst_trades: int | None = None,
    min_return_pct: float | None = None,
    required_promotion_status: str | None = None,
    required_validation_flags: Mapping[str, bool] | None = None,
) -> list[str]:
    """Return human-readable gate failures for explicitly supplied requirements."""

    failures: list[str] = []
    if min_trades is not None:
        trades = _number(report, "trades")
        if trades is None or trades < min_trades:
            failures.append(f"trades {_display_number(trades)} < required {min_trades}")
    if min_catalyst_trades is not None:
        catalyst_trades = _number(report, "catalyst_trade_count")
        if catalyst_trades is None or catalyst_trades < min_catalyst_trades:
            failures.append(
                "catalyst_trade_count "
                f"{_display_number(catalyst_trades)} < required {min_catalyst_trades}"
            )
    if min_return_pct is not None:
        return_pct = _number(report, "return_pct")
        if return_pct is None or return_pct < min_return_pct:
            failures.append(
                f"return_pct {_display_float(return_pct)} < required {min_return_pct:.6f}"
            )
    if required_promotion_status is not None:
        promotion_status = _promotion_status(report)
        if promotion_status != required_promotion_status:
            failures.append(
                "promotion_status "
                f"{promotion_status or '<missing>'} != required {required_promotion_status}"
            )

    validation = report.get("validation")
    validation_map = validation if isinstance(validation, Mapping) else {}
    for field, required in (required_validation_flags or {}).items():
        if required and validation_map.get(field) is not True:
            failures.append(f"validation.{field} is not true")
    return failures


def evaluate_with_profile(
    report: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    min_trades: int | None = None,
    min_catalyst_trades: int | None = None,
    min_return_pct: float | None = None,
    required_promotion_status: str | None = None,
    required_validation_flags: Mapping[str, bool] | None = None,
) -> list[str]:
    """Evaluate a report using a threshold profile plus explicit overrides."""

    profile_flags = _profile_validation_flags(profile)
    override_flags = required_validation_flags or {}
    return evaluate_promotion_report(
        report,
        min_trades=_profile_int(profile, "min_trades", override=min_trades),
        min_catalyst_trades=_profile_int(
            profile,
            "min_catalyst_trades",
            override=min_catalyst_trades,
        ),
        min_return_pct=_profile_float(profile, "min_return_pct", override=min_return_pct),
        required_promotion_status=_profile_str(
            profile,
            "required_promotion_status",
            override=required_promotion_status,
        ),
        required_validation_flags={
            "fixture_backed": override_flags.get("fixture_backed", False)
            or profile_flags.get("fixture_backed", False),
            "no_live_network": override_flags.get("no_live_network", False)
            or profile_flags.get("no_live_network", False),
            "catalyst_opt_in": override_flags.get("catalyst_opt_in", False)
            or profile_flags.get("catalyst_opt_in", False),
            "packet_loaded": override_flags.get("packet_loaded", False)
            or profile_flags.get("packet_loaded", False),
            "no_stale_catalyst_trades": override_flags.get(
                "no_stale_catalyst_trades",
                False,
            )
            or profile_flags.get("no_stale_catalyst_trades", False),
        },
    )


def load_report(path: str) -> Mapping[str, Any]:
    """Load a promotion report from JSON."""

    return _load_report(path)


def load_profile(path: str) -> Mapping[str, Any]:
    """Load a threshold profile from JSON."""

    return _load_profile(path)


def _load_report(path: str) -> Mapping[str, Any]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise typer.BadParameter(f"Unable to read promotion report: {target}") from exc
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid promotion report JSON: {target}") from exc
    if not isinstance(payload, Mapping):
        raise typer.BadParameter("promotion report must be a JSON object")
    return payload


def _load_profile(path: str) -> Mapping[str, Any]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise typer.BadParameter(f"Unable to read threshold profile: {target}") from exc
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid threshold profile JSON: {target}") from exc
    if not isinstance(payload, Mapping):
        raise typer.BadParameter("threshold profile must be a JSON object")
    return payload


def _profile_int(profile: Mapping[str, Any], field: str, *, override: int | None) -> int | None:
    if override is not None:
        return override
    value = profile.get(field)
    if value is None:
        return None
    if isinstance(value, int):
        return value
    raise typer.BadParameter(f"profile field must be an integer: {field}")


def _profile_float(
    profile: Mapping[str, Any],
    field: str,
    *,
    override: float | None,
) -> float | None:
    if override is not None:
        return override
    value = profile.get(field)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raise typer.BadParameter(f"profile field must be numeric: {field}")


def _profile_str(profile: Mapping[str, Any], field: str, *, override: str | None) -> str | None:
    if override is not None:
        return override
    value = profile.get(field)
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise typer.BadParameter(f"profile field must be a non-empty string: {field}")


def _profile_validation_flags(profile: Mapping[str, Any]) -> Mapping[str, bool]:
    flags = profile.get("required_validation_flags", {})
    if not isinstance(flags, Mapping):
        raise typer.BadParameter("profile field must be an object: required_validation_flags")
    parsed: dict[str, bool] = {}
    for key, value in flags.items():
        if not isinstance(key, str) or not isinstance(value, bool):
            raise typer.BadParameter("profile validation flags must map strings to booleans")
        parsed[key] = value
    return parsed


def _number(report: Mapping[str, Any], field: str) -> float | None:
    value = report.get(field)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _promotion_status(report: Mapping[str, Any]) -> str | None:
    catalyst = report.get("catalyst")
    if not isinstance(catalyst, Mapping):
        return None
    status = catalyst.get("promotion_status")
    return status if isinstance(status, str) else None


def _display_number(value: float | None) -> str:
    if value is None:
        return "<missing>"
    if value.is_integer():
        return str(int(value))
    return f"{value:.6f}"


def _display_float(value: float | None) -> str:
    if value is None:
        return "<missing>"
    return f"{value:.6f}"


if __name__ == "__main__":
    app()
