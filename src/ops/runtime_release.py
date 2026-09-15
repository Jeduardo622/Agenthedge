"""Controller-owned release authorization for runtime and final broker admission."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from agents.config import AgentRuntimeConfig
from ops.release_gate import ReleaseDecision, ReleaseTrust, release_decision

if TYPE_CHECKING:
    from ops.artifacts import RuntimeArtifactGuard


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _canonical_evidence(value: object) -> tuple[str, datetime | None]:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        document = json.loads(encoded, parse_constant=_reject_constant)
    except (TypeError, ValueError) as exc:
        raise ValueError("release evidence requires finite JSON") from exc
    issued = None
    if isinstance(document, dict) and isinstance(document.get("payload"), dict):
        raw = document["payload"].get("issued_at")
        if isinstance(raw, str):
            try:
                issued = datetime.fromisoformat(raw)
            except ValueError:
                pass
    return encoded, issued


class _EvidenceCell:
    """Atomic candidate evidence with one immutable owner-selected file source."""

    def __init__(
        self,
        evidence: dict[str, object] | None,
        *,
        trust: ReleaseTrust | None,
        stage: str,
    ) -> None:
        self._json, issued = _canonical_evidence(evidence)
        self._issued_at: datetime | None = None
        if trust is not None and issued is not None:
            try:
                aware = issued.tzinfo is not None and issued.utcoffset() is not None
                authenticated = (
                    aware
                    and release_decision(evidence, trust=trust, stage=stage, now=issued)["passed"]
                )
            except (TypeError, ValueError):
                authenticated = False
            if authenticated:
                self._issued_at = issued
        self._available = self._issued_at is not None
        self._failed_refresh = False
        self._path: Path | None = None
        self._lock = Lock()

    @property
    def evidence_json(self) -> str:
        with self._lock:
            return self._json

    @property
    def path(self) -> Path | None:
        with self._lock:
            return self._path

    @property
    def available(self) -> bool:
        with self._lock:
            return self._available

    def _mark_unavailable(self) -> None:
        with self._lock:
            self._available = False
            self._failed_refresh = True

    def decision(self, *, trust: ReleaseTrust, stage: str, now: datetime) -> ReleaseDecision:
        with self._lock:
            if not self._available:
                return {
                    "stage": stage,
                    "passed": False,
                    "reasons": ["runtime_release_evidence_unavailable"],
                }
            return release_decision(json.loads(self._json), trust=trust, stage=stage, now=now)

    def bind_path(self, path: Path) -> None:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("release evidence source must be a file")
        try:
            candidate = json.loads(resolved.read_bytes(), parse_constant=_reject_constant)
            encoded, _ = _canonical_evidence(candidate)
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("release evidence source requires finite JSON") from exc
        with self._lock:
            if self._path is not None and self._path != resolved:
                raise ValueError("release evidence source is already bound")
            if encoded != self._json:
                raise ValueError("release evidence source differs from installed candidate")
            self._path = resolved

    def refresh(self, *, trust: ReleaseTrust, stage: str, now: datetime, recover: bool) -> None:
        with self._lock:
            path = self._path
        try:
            if path is None:
                raise ValueError("release evidence source is not bound")
            candidate = json.loads(path.read_bytes(), parse_constant=_reject_constant)
            encoded, issued = _canonical_evidence(candidate)
            decision = release_decision(candidate, trust=trust, stage=stage, now=now)
            if not decision["passed"]:
                raise ValueError("replacement release evidence is not currently authorized")
            if issued is None or issued.tzinfo is None or issued.utcoffset() is None:
                raise ValueError("replacement release evidence requires aware issuance")
        except (OSError, TypeError, ValueError) as exc:
            self._mark_unavailable()
            raise ValueError("replacement release evidence is unavailable") from exc
        with self._lock:
            if encoded == self._json:
                if recover:
                    self._available = True
                    self._failed_refresh = False
                return
            if self._issued_at is not None and issued <= self._issued_at:
                self._available = False
                self._failed_refresh = True
                raise ValueError("replacement release evidence must be newer")
            self._json, self._issued_at = encoded, issued
            if recover or not self._failed_refresh:
                self._available = True
            if recover:
                self._failed_refresh = False


@dataclass(frozen=True)
class RuntimeReleaseAuthorization:
    """Copy candidate evidence; retain the actual config to detect later changes.

    The controller must supply trust independently. Message payloads never create
    this capability. Constructing it performs no activation or broker operation.
    """

    config: AgentRuntimeConfig = field(repr=False)
    account_id: str
    trust: ReleaseTrust | None = field(repr=False)
    _evidence: _EvidenceCell = field(repr=False)
    installed_guard: RuntimeArtifactGuard | None = field(default=None, repr=False)

    @classmethod
    def build(
        cls,
        config: AgentRuntimeConfig,
        account_id: str,
        trust: ReleaseTrust | None,
        evidence: dict[str, object] | None,
    ) -> RuntimeReleaseAuthorization:
        stage = "live_start" if config.execution_mode == "live" else "paper_start"
        return cls(
            config,
            account_id,
            trust,
            _EvidenceCell(evidence, trust=trust, stage=stage),
        )

    @property
    def _evidence_json(self) -> str:
        return self._evidence.evidence_json

    @property
    def evidence_path(self) -> Path | None:
        return self._evidence.path

    def bind_evidence_path(self, path: Path) -> None:
        self._evidence.bind_path(path)

    def refresh_evidence(self, *, now: datetime, recover: bool = False) -> None:
        if self.trust is None:
            raise ValueError("independent release trust required")
        stage = "live_start" if self.trust.expected.mode == "live" else "paper_start"
        self._evidence.refresh(trust=self.trust, stage=stage, now=now, recover=recover)

    def check(self, *, account_id: str, mode: str, now: datetime) -> ReleaseDecision:
        stage = "live_start" if mode == "live" else "paper_start"
        if self.installed_guard is not None:
            from ops.artifacts import RuntimeArtifactGuard

            try:
                if type(self.installed_guard) is not RuntimeArtifactGuard:
                    raise ValueError("concrete installed artifact guard required")
                self.installed_guard.require_current()
                now = max(now, self.installed_guard.current_time())
            except Exception:
                return {"stage": stage, "passed": False, "reasons": ["installed_artifacts_changed"]}
        if (
            mode not in {"paper_broker", "live"}
            or mode != self.config.execution_mode
            or account_id != self.account_id
            or self.trust is None
            or self.trust.expected.account_id != account_id
            or self.trust.expected.mode != mode
            or self.trust.expected.config_hash != self.config.release_config_hash()
        ):
            return {
                "stage": stage,
                "passed": False,
                "reasons": ["runtime_release_binding_required"],
            }
        return self._evidence.decision(trust=self.trust, stage=stage, now=now)
