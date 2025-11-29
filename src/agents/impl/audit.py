"""Audit agent producing weekly compliance/risk summaries."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, cast

from observability.state import ObservabilityState

from ..base import BaseAgent
from ..context import AgentContext


class AuditAgent(BaseAgent):
    """Reads the JSONL audit log and emits weekly summary reports."""

    def __init__(self, context: AgentContext) -> None:
        super().__init__(context)
        extras = context.extras or {}
        audit_path = extras.get("audit_path")
        self._audit_path = Path(audit_path or "storage/audit/runtime_events.jsonl")
        report_dir = extras.get("audit_report_dir")
        self._report_dir = Path(report_dir or "storage/audit/reports")
        self._report_dir.mkdir(parents=True, exist_ok=True)
        observability_state = extras.get("observability_state")
        self._observability_state = (
            observability_state if isinstance(observability_state, ObservabilityState) else None
        )
        self._index_path = self._report_dir / "index.json"
        self._last_report_week = self._load_last_week()

    def tick(self) -> None:
        now = datetime.now(timezone.utc)
        if now.weekday() != 0:  # Only run Mondays
            return
        target_week = self._week_label(now - timedelta(days=3))
        if target_week == self._last_report_week:
            return
        report = self._build_report(target_week)
        if report is None:
            return
        self._write_report(target_week, report)

    # No setup/teardown hooks required

    def _build_report(self, iso_week: str) -> Dict[str, Any] | None:
        if not self._audit_path.exists():
            return None
        counter: Counter[str] = Counter()
        breaches: list[Dict[str, Any]] = []
        alerts: list[Dict[str, Any]] = []
        week_year, week_num = iso_week.split("-W")
        events = self._read_events()
        for event in events:
            timestamp = self._parse_timestamp(event.get("timestamp"))
            if not timestamp:
                continue
            iso_tuple = timestamp.isocalendar()
            if str(iso_tuple[0]) != week_year or f"{iso_tuple[1]:02d}" != week_num:
                continue
            action = str(event.get("action", "unknown"))
            counter[action] += 1
            payload = event.get("payload") or {}
            if "alert" in action or action.endswith("kill_switch"):
                alerts.append(
                    {"action": action, "payload": payload, "timestamp": event.get("timestamp")}
                )
            if action.endswith("kill_switch") or action in {"risk_stress_breach", "risk_reject"}:
                breaches.append(
                    {"action": action, "payload": payload, "timestamp": event.get("timestamp")}
                )
        if not counter:
            return None
        return {
            "week": iso_week,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "counts": dict(counter),
            "breaches": breaches,
            "alerts": alerts[-50:],
        }

    def _write_report(self, iso_week: str, report: Dict[str, Any]) -> None:
        output_path = self._report_dir / f"weekly_{iso_week}.json"
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        index_payload = {
            "last_report_week": iso_week,
            "report_path": str(output_path),
            "generated_at": report["generated_at"],
        }
        self._index_path.write_text(json.dumps(index_payload, indent=2), encoding="utf-8")
        self._last_report_week = iso_week
        if self._observability_state:
            self._observability_state.record_audit_report(index_payload)
        self.audit("audit_weekly_report", {"week": iso_week, "report_path": str(output_path)})

    def _read_events(self) -> Iterable[Dict[str, Any]]:
        with self._audit_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def _load_last_week(self) -> str | None:
        if not self._index_path.exists():
            return None
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return cast(str | None, data.get("last_report_week"))

    def _week_label(self, value: datetime) -> str:
        iso = value.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    def _parse_timestamp(self, raw: Any) -> datetime | None:
        if not isinstance(raw, str):
            return None
        normalized = raw.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None


__all__ = ["AuditAgent"]
