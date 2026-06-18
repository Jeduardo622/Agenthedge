"""Promotion gate for paper rollout evidence artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

app = typer.Typer(help="Evaluate paper_rollout_evidence artifacts")

DEFAULT_PROFILE = "config/promotion-gates/paper_rollout.json"


@app.command()
def main(
    evidence: str = typer.Option(
        ...,
        "--evidence",
        help="Path to paper_rollout_evidence_<timestamp>.json",
    ),
    profile: str = typer.Option(
        DEFAULT_PROFILE,
        "--profile",
        help="JSON gate profile to apply.",
    ),
) -> None:
    payload = load_evidence(evidence)
    profile_config = load_profile(profile)
    failures = evaluate_with_profile(payload, profile_config)
    if failures:
        typer.echo(f"PAPER_ROLLOUT_GATE_FAIL {evidence}")
        for failure in failures:
            typer.echo(f"- {failure}")
        raise typer.Exit(1)
    typer.echo(f"PAPER_ROLLOUT_GATE_PASS {evidence}")


def evaluate_with_profile(
    evidence: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> list[str]:
    failures: list[str] = []
    required_checks = _profile_required_checks(profile)
    max_age_hours = _profile_max_age_hours(profile)
    failures.extend(evaluate_evidence(evidence, required_checks=required_checks, now=now))
    if max_age_hours is not None:
        failures.extend(_evaluate_evidence_age(evidence, max_age_hours=max_age_hours, now=now))
    return failures


def evaluate_evidence(
    evidence: Mapping[str, Any],
    *,
    required_checks: Sequence[str],
    now: datetime | None = None,
) -> list[str]:
    failures: list[str] = []
    artifact_type = evidence.get("artifact_type")
    if artifact_type != "paper_rollout_evidence":
        failures.append(f"artifact_type {artifact_type or '<missing>'} != paper_rollout_evidence")
    status = evidence.get("status")
    if status != "passed":
        failures.append(f"evidence status {status or '<missing>'} != passed")
    signature = evidence.get("rehearsal_signature")
    signature_map = signature if isinstance(signature, Mapping) else {}
    if signature_map.get("algorithm") != "sha256":
        failures.append("rehearsal_signature.algorithm is not sha256")
    if not signature_map.get("digest"):
        failures.append("rehearsal_signature.digest is missing")
    check_map = _checks_by_name(evidence.get("checks"))
    for check_name in required_checks:
        check = check_map.get(check_name)
        if check is None:
            failures.append(f"check.{check_name} is missing")
            continue
        check_status = check.get("status")
        if check_status != "passed":
            failures.append(f"check.{check_name} status {check_status or '<missing>'} != passed")
    return failures


def load_evidence(path: str) -> Mapping[str, Any]:
    return _load_json_object(path, artifact_name="paper rollout evidence")


def load_profile(path: str) -> Mapping[str, Any]:
    return _load_json_object(path, artifact_name="paper rollout gate profile")


def _load_json_object(path: str, *, artifact_name: str) -> Mapping[str, Any]:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise typer.BadParameter(f"Unable to read {artifact_name}: {target}") from exc
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid {artifact_name} JSON: {target}") from exc
    if not isinstance(payload, Mapping):
        raise typer.BadParameter(f"{artifact_name} must be a JSON object")
    return payload


def _checks_by_name(value: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list):
        return {}
    checks: dict[str, Mapping[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if isinstance(name, str):
            checks[name] = item
    return checks


def _profile_required_checks(profile: Mapping[str, Any]) -> list[str]:
    value = profile.get("required_checks")
    if not isinstance(value, list):
        raise typer.BadParameter("profile field must be a list: required_checks")
    required: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise typer.BadParameter("profile required_checks entries must be non-empty strings")
        required.append(item.strip())
    return required


def _profile_max_age_hours(profile: Mapping[str, Any]) -> float | None:
    value = profile.get("max_evidence_age_hours")
    if value is None:
        return None
    if not isinstance(value, (int, float)) or value <= 0:
        raise typer.BadParameter("profile max_evidence_age_hours must be a positive number")
    return float(value)


def _evaluate_evidence_age(
    evidence: Mapping[str, Any],
    *,
    max_age_hours: float,
    now: datetime | None,
) -> list[str]:
    created_at = evidence.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        return ["evidence created_at is missing"]
    try:
        created = _parse_datetime(created_at)
    except ValueError:
        return [f"evidence created_at is invalid: {created_at}"]
    current = now or datetime.now(timezone.utc)
    age_hours = (current - created).total_seconds() / 3600
    if age_hours < 0:
        return [f"evidence created_at is in the future: {created_at}"]
    if age_hours > max_age_hours:
        return [f"evidence age {age_hours:.2f}h > max {max_age_hours:.2f}h"]
    return []


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


if __name__ == "__main__":
    app()
