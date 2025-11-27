"""JSONL audit sink storing agent events for replay."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, MutableMapping


class JsonlAuditSink:
    """Thread-safe JSONL writer for audit events."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, action: str, payload: Mapping[str, object] | None = None) -> None:
        record: MutableMapping[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "payload": dict(payload or {}),
        }
        line = json.dumps(record, ensure_ascii=True)
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
