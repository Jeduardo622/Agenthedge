"""Quarantine store for suspect ingestion payloads."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping


@dataclass(frozen=True)
class QuarantineRecord:
    quarantine_id: str
    symbol: str
    data_type: str
    reason: str
    payload: Mapping[str, Any]
    timestamp: str
    released: bool = False


class QuarantineStore:
    """Persists quarantine events in JSONL."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._active: set[tuple[str, str]] = set()
        self._load_active()

    def quarantine(
        self, *, symbol: str, data_type: str, reason: str, payload: Mapping[str, Any]
    ) -> QuarantineRecord:
        record = QuarantineRecord(
            quarantine_id=str(uuid.uuid4()),
            symbol=symbol,
            data_type=data_type,
            reason=reason,
            payload=dict(payload),
            timestamp=datetime.now(timezone.utc).isoformat(),
            released=False,
        )
        self._append(record)
        self._active.add((symbol.upper(), data_type))
        return record

    def release(self, *, symbol: str, data_type: str, reason: str = "manual_release") -> None:
        record = QuarantineRecord(
            quarantine_id=str(uuid.uuid4()),
            symbol=symbol,
            data_type=data_type,
            reason=reason,
            payload={},
            timestamp=datetime.now(timezone.utc).isoformat(),
            released=True,
        )
        self._append(record)
        self._active.discard((symbol.upper(), data_type))

    def is_quarantined(self, *, symbol: str, data_type: str) -> bool:
        return (symbol.upper(), data_type) in self._active

    def list_records(self, *, include_released: bool = True) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        records: List[Dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                item = json.loads(line)
                if not include_released and bool(item.get("released")):
                    continue
                records.append(item)
        return records

    def _append(self, record: QuarantineRecord) -> None:
        line = json.dumps(record.__dict__, sort_keys=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")

    def _load_active(self) -> None:
        if not self.path.exists():
            return
        for record in self.list_records(include_released=True):
            symbol = str(record.get("symbol", "")).upper()
            data_type = str(record.get("data_type", ""))
            if not symbol or not data_type:
                continue
            key = (symbol, data_type)
            if bool(record.get("released")):
                self._active.discard(key)
            else:
                self._active.add(key)
