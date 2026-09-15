"""Authenticated, stage-specific release evidence. This module performs no activation.

The controller supplies identity, issuer keys and the qualification account independently
of the candidate dossier. Issuers attest observations; a signature does not run a test.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Mapping, TypedDict

from .calendar import USTradingCalendar


@dataclass(frozen=True)
class ReleaseIdentity:
    sha: str
    account_id: str
    mode: str
    config_hash: str
    policy_hash: str
    strategy_hash: str
    data_hash: str

    def __post_init__(self) -> None:
        if (
            not _hex(self.sha, 40)
            or not isinstance(self.account_id, str)
            or not self.account_id.strip()
        ):
            raise ValueError("release requires exact SHA and explicit account")
        if self.mode not in {"simulated", "paper_broker", "live"}:
            raise ValueError("unsupported release mode")
        for value in (self.config_hash, self.policy_hash, self.strategy_hash, self.data_hash):
            if not _hex(value, 64):
                raise ValueError("release requires SHA-256 identity hashes")


@dataclass(frozen=True)
class ReleaseTrust:
    """Controller-owned trust, supplied independently of candidate evidence."""

    expected: ReleaseIdentity
    trusted_keys: Mapping[str, bytes] = field(repr=False)
    paper_account_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.expected, ReleaseIdentity):
            raise ValueError("independent release identity required")
        if any(
            not isinstance(name, str) or not name or not isinstance(key, bytes) or len(key) < 32
            for name, key in self.trusted_keys.items()
        ):
            raise ValueError("invalid approved issuer keyring")
        object.__setattr__(self, "trusted_keys", MappingProxyType(dict(self.trusted_keys)))


class ReleaseDecision(TypedDict):
    stage: str
    passed: bool
    reasons: list[str]


def release_decision(
    evidence: dict[str, object] | None,
    *,
    trust: ReleaseTrust | None,
    stage: str,
    now: datetime,
) -> ReleaseDecision:
    """Return one sanitized decision for CLI, configuration and controller consumers."""
    reasons: tuple[str, ...]
    if trust is None:
        passed, reasons = False, ("independent_release_trust_required",)
    else:
        passed, reasons = evaluate_release(
            evidence if evidence is not None else {},
            stage=stage,
            expected=trust.expected,
            now=now,
            trusted_keys=trust.trusted_keys,
            paper_account_id=trust.paper_account_id,
        )
    return {"stage": stage, "passed": passed, "reasons": list(reasons)}


def release_policy() -> dict[str, object]:
    """Return a fresh policy value; callers cannot mutate the process policy."""
    return {
        "schema_version": 1,
        "max_dossier_age_seconds": 86400,
        "max_preflight_age_seconds": 300,
        "stages": {
            "paper_start": ["G0", "G1", "G2"],
            "dependable_paper": ["G0", "G1", "G2", "G4"],
            "live_start": ["G0", "G1", "G2", "G3", "G4", "G5"],
            "closeout": ["G0", "G1", "G2", "G3", "G4", "G5", "G6"],
        },
        "minimum_sessions": {
            "paper_start": 0,
            "dependable_paper": 5,
            "live_start": 20,
            "closeout": 20,
        },
        "checks": {
            "G0": ["baseline", "regression_matrix", "mandate", "policy_manifest", "data_manifest"],
            "G1": [
                "decimal_accounting",
                "crash_replay",
                "broker_history_reconciliation",
                "migration_rollback",
            ],
            "G2": [
                "session_loss",
                "aggregate_reservations",
                "stale_feed_veto",
                "cancel_races",
                "persistent_halt",
                "observed_rollback",
                "current_preflight",
            ],
            "G3": [
                "runtime_replay_parity",
                "causal_fills",
                "bias_tests",
                "frozen_holdout",
                "approved_objective",
            ],
            "G4": ["paper_operations"],
            "G5": [
                "representative_sessions",
                "fault_drills",
                "account_readiness",
                "approved_caps",
                "owner_authorization",
            ],
            "G6": [
                "observed_pilot",
                "operator_workflows",
                "restore_runbook",
                "residual_risk_acceptance",
            ],
        },
    }


def evaluate_release(
    evidence: dict[str, object],
    *,
    stage: str,
    expected: ReleaseIdentity,
    now: datetime,
    trusted_keys: Mapping[str, bytes] | None = None,
    paper_account_id: str | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Validate an issuer-authenticated dossier; missing trust always denies.

    The optional trust arguments must come from controller configuration, never the
    evidence. They read no environment or files and have no default deployed secrets.
    """
    try:
        current = _time(now)
        policy = release_policy()
        stages = _mapping(policy["stages"])
        if stage not in stages:
            return False, ("unknown_stage",)
        envelope = _mapping(evidence)
        payload = _mapping(envelope["payload"])
        signature = _mapping(envelope["signature"])
        issuer = signature.get("issuer")
        key = (trusted_keys or {}).get(issuer) if isinstance(issuer, str) else None
        if not isinstance(key, bytes) or len(key) < 32:
            return False, ("trusted_issuer_required",)
        supplied = signature.get("digest")
        if signature.get("algorithm") != "hmac-sha256" or not _hex(supplied, 64):
            return False, ("signature_invalid",)
        computed = hmac.new(key, _encoded(payload), "sha256").hexdigest()
        if not hmac.compare_digest(computed, str(supplied)):
            return False, ("signature_invalid",)
        identity = asdict(expected)
        reasons: list[str] = []
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
            reasons.append("unsupported_schema")
        if payload.get("identity") != identity:
            reasons.append("release_identity_mismatch")
        if payload.get("policy_hash") != _digest(policy):
            reasons.append("gate_policy_mismatch")
        issued, expires = _time(payload["issued_at"]), _time(payload["expires_at"])
        max_age = timedelta(seconds=int(str(policy["max_dossier_age_seconds"])))
        if (
            not issued <= current < expires
            or expires - issued > max_age
            or current - issued > max_age
        ):
            reasons.append("dossier_time_invalid")
        artifacts = _mapping(payload["artifacts"])
        gates = _mapping(payload["gates"])
        checks = _mapping(policy["checks"])
        for gate in _strings(stages[stage]):
            refs = _mapping(gates.get(gate, {}))
            for name in _strings(checks[gate]):
                try:
                    artifact = _artifact(artifacts, refs.get(name), name, identity, issued)
                    if name == "current_preflight" and current - _time(
                        artifact["observed_at"]
                    ) > timedelta(seconds=int(str(policy["max_preflight_age_seconds"]))):
                        raise ValueError("stale preflight")
                except (ValueError, TypeError, KeyError):
                    reasons.append(f"{gate}:{name}:invalid_evidence")
        minimum = int(str(_mapping(policy["minimum_sessions"])[stage]))
        if minimum:
            reasons.extend(
                _sessions(
                    payload.get("sessions"), minimum, identity, issued, paper_account_id, artifacts
                )
            )
        return not reasons, tuple(reasons)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return False, ("malformed_evidence",)


def _artifact(
    artifacts: dict[str, object], ref: object, kind: str, identity: dict[str, str], issued: datetime
) -> dict[str, object]:
    if not _hex(ref, 64):
        raise ValueError("artifact reference required")
    item = _mapping(artifacts[str(ref)])
    if _digest(item) != ref or item.get("kind") != kind or item.get("identity") != identity:
        raise ValueError("artifact integrity or identity mismatch")
    if item.get("passed") is not True or not _mapping(item.get("details")):
        raise ValueError("artifact result/details unavailable")
    if _time(item["observed_at"]) > issued:
        raise ValueError("future artifact")
    return item


def _sessions(
    value: object,
    minimum: int,
    identity: dict[str, str],
    issued: datetime,
    account: str | None,
    artifacts: dict[str, object],
) -> list[str]:
    if not isinstance(account, str) or not account.strip():
        return ["qualification_account_required"]
    if not isinstance(value, list) or len(value) < minimum:
        return ["insufficient_observed_sessions"]
    seen: set[str] = set()
    calendar = USTradingCalendar()
    for raw in value:
        try:
            item = _mapping(raw)
            session_id = str(item["session_id"])
            day = date.fromisoformat(session_id)
            if day.isoformat() != session_id or session_id in seen:
                raise ValueError("duplicate or malformed session")
            seen.add(session_id)
            bounds = calendar.session_bounds(day)
            opened, closed = _time(item["opened_at"]), _time(item["closed_at"])
            if bounds is None or opened > bounds[0] or closed < bounds[1] or closed > issued:
                raise ValueError("incomplete market session")
            if (
                opened.date() != day
                or closed.date() != day
                or _time(item["safety_qualified_at"]) >= opened
            ):
                raise ValueError("session before safety qualification")
            if (
                item.get("account_id") != account
                or item.get("mode") != "paper_broker"
                or item.get("identity") != identity
            ):
                raise ValueError("session identity mismatch")
            if any(item.get(key) is not True for key in ("complete", "clean", "observed")):
                raise ValueError("session not observed/complete/clean")
            if item.get("mismatches") != [] or item.get("unresolved_orders") != []:
                raise ValueError("unresolved reconciliation")
            if type(item.get("trade_count")) is not int or int(str(item["trade_count"])) < 0:
                raise ValueError("trade count unavailable")
            receipt = _artifact(
                artifacts, item.get("closeout_hash"), "session_closeout", identity, issued
            )
            expected_details = {k: v for k, v in item.items() if k != "closeout_hash"}
            if receipt["details"] != expected_details or _time(receipt["observed_at"]) < closed:
                raise ValueError("closeout does not bind observed session")
        except (ValueError, KeyError, TypeError, RuntimeError):
            return ["invalid_observed_session"]
    return []


def _time(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("aware time required")
    return value.astimezone(timezone.utc)


def _hex(value: object, size: int) -> bool:
    return isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{size}}}", value) is not None


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("JSON object required")
    return value


def _strings(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("string list required")
    return value


def _encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()
