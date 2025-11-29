from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict

from agents.context import AgentContext
from agents.impl.audit import AuditAgent
from observability.state import ObservabilityState


def _build_context(tmp_path, extras: Dict[str, Any]) -> AgentContext:
    ingestion = SimpleNamespace()
    return AgentContext.build_default(
        name="audit",
        ingestion=ingestion,
        extras=extras,
    )


def test_audit_agent_generates_weekly_report(tmp_path) -> None:
    audit_log = tmp_path / "audit.jsonl"
    report_dir = tmp_path / "reports"
    report_dir.mkdir()

    now = datetime.now(timezone.utc)
    sample_event = {
        "timestamp": now.isoformat(),
        "action": "risk_reject",
        "payload": {"symbol": "AAPL"},
    }
    audit_log.write_text(json.dumps(sample_event) + "\n", encoding="utf-8")

    state = ObservabilityState()
    ctx = _build_context(
        tmp_path,
        {
            "audit_path": audit_log,
            "audit_report_dir": report_dir,
            "observability_state": state,
        },
    )
    agent = AuditAgent(ctx)
    week_label = agent._week_label(now)
    report = agent._build_report(week_label)
    assert report is not None
    agent._write_report(week_label, report)

    written = json.loads((report_dir / f"weekly_{week_label}.json").read_text(encoding="utf-8"))
    assert written["counts"]["risk_reject"] == 1
    snapshot = state.snapshot()
    assert snapshot["audit"]["last_report_week"] == week_label
