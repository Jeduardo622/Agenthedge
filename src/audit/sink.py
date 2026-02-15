"""JSONL audit sink storing agent events for replay."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping


class JsonlAuditSink:
    """Thread-safe JSONL writer for audit events."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_hash: str | None = None

    def __call__(
        self,
        action: str,
        payload: Mapping[str, object] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        payload_dict = dict(payload or {})
        metadata_dict = dict(metadata or {})
        context_ref = _resolve_context_ref(payload_dict)
        approvals = (
            payload_dict.get("approvals")
            if isinstance(payload_dict.get("approvals"), dict)
            else None
        )
        inputs = (
            payload_dict.get("data_metadata")
            if isinstance(payload_dict.get("data_metadata"), dict)
            else None
        )
        record: MutableMapping[str, object] = {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_id": metadata_dict.get("agent_id"),
            "run_id": metadata_dict.get("run_id"),
            "environment": metadata_dict.get("environment"),
            "event_type": action,
            "context_ref": context_ref,
            "inputs": inputs,
            "approvals": approvals,
            "action": action,
            "payload": payload_dict,
            "prev_hash": self._last_hash,
        }
        record_hash = _hash_record(record)
        record["hash"] = record_hash
        line = _serialize_record(record)
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
            self._last_hash = record_hash

    @property
    def path(self) -> Path:
        return self._path


def _resolve_context_ref(payload: Mapping[str, object]) -> str | None:
    for key in ("decision_id", "proposal_id", "directive_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _hash_record(record: Mapping[str, object]) -> str:
    payload = _serialize_record(record)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _serialize_record(record: Mapping[str, object]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def verify_jsonl_hash_chain(path: str | Path) -> tuple[bool, list[str]]:
    target = Path(path)
    if not target.exists():
        return False, [f"audit file not found: {target}"]
    errors: list[str] = []
    previous_hash: str | None = None
    with target.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                errors.append(f"line {index}: invalid JSON ({exc})")
                continue
            if not isinstance(record, dict):
                errors.append(f"line {index}: expected object payload")
                continue
            actual_prev = record.get("prev_hash")
            if actual_prev != previous_hash:
                errors.append(
                    "line %s: prev_hash mismatch (expected %r, got %r)"
                    % (index, previous_hash, actual_prev)
                )
            claimed_hash = record.get("hash")
            if not isinstance(claimed_hash, str) or not claimed_hash:
                errors.append(f"line {index}: missing hash")
                previous_hash = None
                continue
            payload = dict(record)
            payload.pop("hash", None)
            computed_hash = _hash_record(payload)
            if computed_hash != claimed_hash:
                errors.append(f"line {index}: hash mismatch")
            previous_hash = claimed_hash
    return len(errors) == 0, errors
