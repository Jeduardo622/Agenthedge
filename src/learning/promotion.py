"""Controller-owned signed strategy evidence, checked again at each activation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from ops.release_gate import ReleaseTrust, release_decision
from portfolio.accounting import as_decimal


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class StrategyAcceptance:
    trust: ReleaseTrust
    evidence: bytes
    manifest: bytes
    clock: Callable[[], datetime] = _now

    def __post_init__(self) -> None:
        if not isinstance(self.trust, ReleaseTrust) or not callable(self.clock):
            raise TypeError("independent release trust and controller clock required")
        if type(self.evidence) is not bytes or type(self.manifest) is not bytes:
            raise TypeError("immutable signed evidence and strategy artifact bytes required")

    def require_candidate(self, strategy: str, weight: object, *, safety_revision: int = 0) -> str:
        identity = self.trust.expected
        if hashlib.sha256(self.manifest).hexdigest() != identity.strategy_hash:
            raise ValueError("accepted strategy artifact hash mismatch")
        manifest = json.loads(self.manifest)
        evidence = json.loads(self.evidence)
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or not isinstance(manifest.get("strategy_weights"), dict)
            or not isinstance(evidence, dict)
        ):
            raise ValueError("signed strategy weight manifest required")
        revisions = manifest.get("strategy_safety_revisions", {})
        if not isinstance(revisions, dict):
            raise ValueError("signed strategy safety revision mapping required")
        approved_revision = revisions.get(strategy, 0)
        if (
            type(safety_revision) is not int
            or safety_revision < 0
            or type(approved_revision) is not int
            or approved_revision != safety_revision
        ):
            raise ValueError("new acceptance required for current strategy safety revision")
        approved = as_decimal(manifest["strategy_weights"].get(strategy))
        if not Decimal("0") < approved <= Decimal("2.5") or approved != as_decimal(weight):
            raise ValueError("new acceptance required for changed candidate weight")
        stage = "live_start" if identity.mode == "live" else "paper_start"
        decision = release_decision(evidence, trust=self.trust, stage=stage, now=self.clock())
        if not decision["passed"]:
            raise ValueError("accepted strategy release evidence is missing, invalid or expired")
        return identity.strategy_hash
