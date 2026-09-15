"""Explicit controller trust loading; no dotenv, runtime, network or state mutation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from ops.release_gate import ReleaseDecision, ReleaseIdentity, ReleaseTrust, release_decision
from risk.runtime_sources import SessionControlConfig


@dataclass(frozen=True)
class WorkerAuthority:
    trust: ReleaseTrust = field(repr=False)
    evidence: bytes = field(repr=False)

    def check(self, *, now: datetime) -> ReleaseDecision:
        stage = "live_start" if self.trust.expected.mode == "live" else "paper_start"
        return release_decision(json.loads(self.evidence), trust=self.trust, stage=stage, now=now)


def load_worker_authority(
    trust_path: Path,
    evidence_path: Path,
    *,
    environment: Mapping[str, str],
) -> WorkerAuthority:
    """Read owner-managed identity/key references separately from candidate evidence.

    The trust file is an operator authority boundary, not a file supplied by a
    candidate release. Existing named environment values supply issuer keys; their
    values are never returned as printable configuration or added to any file.
    Expired evidence remains loadable for recovery; ``check`` never authorizes it.
    """
    if trust_path.resolve(strict=True) == evidence_path.resolve(strict=True):
        raise ValueError("owner trust and candidate evidence require distinct files")
    try:
        document = json.loads(trust_path.read_bytes())
        evidence = evidence_path.read_bytes()
        candidate = json.loads(evidence)
    except (OSError, ValueError) as exc:
        raise ValueError("worker authority files must contain readable JSON") from exc
    if (
        not isinstance(document, dict)
        or set(document)
        != {"schema_version", "identity", "issuer_key_environment", "paper_account_id"}
        or type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or not isinstance(document["identity"], dict)
        or not isinstance(candidate, dict)
    ):
        raise ValueError("explicit owner worker trust schema required")
    try:
        identity = ReleaseIdentity(**document["identity"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid independent worker release identity") from exc
    if (
        identity.mode not in {"paper_broker", "live"}
        or identity.account_id != identity.account_id.strip()
    ):
        raise ValueError("canonical broker account and mode required")
    references = document["issuer_key_environment"]
    if not isinstance(references, dict) or not references:
        raise ValueError("explicit issuer environment references required")
    keys = {}
    for issuer, variable in references.items():
        if (
            not isinstance(issuer, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+", issuer) is None
            or not isinstance(variable, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable) is None
        ):
            raise ValueError("invalid issuer environment reference")
        value = environment.get(variable)
        if not isinstance(value, str) or len(value.encode()) < 32:
            raise ValueError("configured issuer key unavailable or too short")
        keys[issuer] = value.encode()
    paper = document["paper_account_id"]
    if paper is not None and (not isinstance(paper, str) or not paper or paper != paper.strip()):
        raise ValueError("canonical qualification paper account required")
    return WorkerAuthority(ReleaseTrust(identity, keys, paper), evidence)


def parse_session_controls(value: object) -> SessionControlConfig:
    """Parse explicit approved session settings; no environment or fallback values."""
    if not isinstance(value, dict) or set(value) != {
        "max_mark_age_seconds",
        "boundary_grace_seconds",
        "window_sessions",
        "max_drawdown",
        "control_timeout_seconds",
    }:
        raise ValueError("complete approved session controls required")

    def positive(name: str) -> Decimal:
        try:
            result = Decimal(str(value[name]))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("finite positive session controls required") from exc
        if not result.is_finite() or result <= 0:
            raise ValueError("finite positive session controls required")
        return result

    if type(value["window_sessions"]) is not int or value["window_sessions"] <= 0:
        raise ValueError("positive integer session window required")
    try:
        return SessionControlConfig(
            max_mark_age=timedelta(seconds=float(positive("max_mark_age_seconds"))),
            boundary_grace=timedelta(seconds=float(positive("boundary_grace_seconds"))),
            window_sessions=value["window_sessions"],
            max_drawdown=positive("max_drawdown"),
            control_timeout=timedelta(seconds=float(positive("control_timeout_seconds"))),
        )
    except (OverflowError, ArithmeticError) as exc:
        raise ValueError("finite bounded session controls required") from exc
