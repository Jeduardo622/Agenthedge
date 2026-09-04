"""JSONL audit sink storing agent events for replay."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping, MutableMapping

_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class JsonlAuditSink:
    """Thread- and process-safe JSONL writer for audit events."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._canonical_path = self._path.resolve(strict=False)
        self._lock = _path_lock(self._canonical_path)
        with self._lock, _interprocess_lock(self._canonical_path):
            self._last_hash = _initialize_last_hash(self._canonical_path)

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
        with self._lock, _interprocess_lock(self._canonical_path):
            self._last_hash = _initialize_last_hash(self._canonical_path)
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
            with self._canonical_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._last_hash = record_hash

    @property
    def path(self) -> Path:
        return self._path


def _path_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(str(path))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


@contextmanager
def _interprocess_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    try:
        handle = lock_path.open("a+b")
    except OSError as exc:
        raise OSError(f"unable to open audit lock for {path}: {exc}") from exc
    try:
        _acquire_file_lock(handle)
    except OSError as exc:
        handle.close()
        raise OSError(f"unable to acquire audit lock for {path}: {exc}") from exc
    try:
        yield
    finally:
        try:
            _release_file_lock(handle)
        finally:
            handle.close()


def _acquire_file_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        msvcrt = importlib.import_module("msvcrt")

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        locking = getattr(msvcrt, "locking")
        locking(handle.fileno(), getattr(msvcrt, "LK_LOCK"), 1)
        return

    fcntl = importlib.import_module("fcntl")
    flock = getattr(fcntl, "flock")
    flock(handle.fileno(), getattr(fcntl, "LOCK_EX"))


def _release_file_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        msvcrt = importlib.import_module("msvcrt")

        handle.seek(0)
        locking = getattr(msvcrt, "locking")
        locking(handle.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
        return

    fcntl = importlib.import_module("fcntl")
    flock = getattr(fcntl, "flock")
    flock(handle.fileno(), getattr(fcntl, "LOCK_UN"))


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


def _initialize_last_hash(path: Path) -> str | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    ok, errors = verify_jsonl_hash_chain(path)
    if not ok:
        details = "; ".join(errors[:3]) if errors else "unknown hash-chain failure"
        raise ValueError(
            "existing audit chain is invalid; resolve with scripts/cutover_audit_chain.py "
            "or scripts/migrate_audit_chain.py before writing more events. "
            f"details: {details}"
        )
    return _read_last_hash(path)


def _read_last_hash(path: Path) -> str | None:
    last_hash: str | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            record = json.loads(raw)
            if isinstance(record, dict):
                candidate = record.get("hash")
                if isinstance(candidate, str) and candidate:
                    last_hash = candidate
    return last_hash


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
