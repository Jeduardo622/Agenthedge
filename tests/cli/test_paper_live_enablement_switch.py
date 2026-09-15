from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def test_live_enablement_switch_clean_preflight_still_requires_release_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_enablement_switch

    artifact_dir = tmp_path / "audit"
    review_path = artifact_dir / "paper_live_enablement_final_review_20260622T182100Z.json"
    _write_ready_final_review(review_path)
    decision_path = (
        artifact_dir / "paper_live_enablement_final_review_decision_20260622T182200Z.json"
    )
    _write_json(
        decision_path,
        {
            "artifact_type": "paper_live_enablement_final_review_decision",
            "created_at": "2026-06-22T18:22:00+00:00",
            "outcome": "approve_live_enablement_switch_implementation",
            "approver_role": "operations",
            "reason": "Approved supervised switch implementation.",
            "artifact_refs": [str(review_path)],
            "live_trading_enabled": False,
            "broker_mutation": False,
            "runtime_config_mutation": False,
            "scheduler_mutation": False,
            "env_var_mutation": False,
        },
    )
    monkeypatch.setattr(paper_live_enablement_switch, "_timestamp", lambda: "20260622T182300Z")

    packet = paper_live_enablement_switch.build_switch_packet(
        artifact_dir=artifact_dir,
        env=_ready_live_env(),
        broker_adapter=_CleanLiveBroker(),
        scheduler_state_provider=lambda: {"enabled": False, "source": "test"},
        apply=False,
        now=datetime(2026, 6, 22, 18, 23, tzinfo=timezone.utc),
    )

    assert packet["artifact_type"] == "paper_live_enablement_switch"
    assert packet["outcome"] == "blocked_with_reasons"
    assert packet["release_gate"]["reasons"] == ["independent_release_trust_required"]
    assert packet["dry_run"] is True
    assert packet["apply_requested"] is False
    assert packet["live_switch_applied"] is False
    assert packet["scheduler_mutation"] is False
    assert packet["fresh_preflight"]["status"] == "failed"
    assert packet["switch_diff"]["env_var_changes"][0]["name"] == "EXECUTION_MODE"
    assert packet["switch_diff"]["env_var_changes"][0]["from"] == "paper_broker"
    assert packet["switch_diff"]["env_var_changes"][0]["to"] == "live"
    assert "rollback" in packet["rollback_packet"]["rollback_command"]
    assert "--apply" in packet["rollback_packet"]["rollback_command"]
    assert "ROLLBACK LIVE SWITCH" in packet["rollback_packet"]["rollback_command"]
    assert packet["switch_transcript_artifact"].endswith(
        "paper_live_enablement_switch_20260622T182300Z.json"
    )
    markdown = Path(packet["switch_transcript_markdown_artifact"]).read_text(encoding="utf-8")
    assert "LIVE_ENABLEMENT_SWITCH" in markdown
    assert "outcome: blocked_with_reasons" in markdown


def test_live_enablement_switch_blocks_without_approved_final_decision(tmp_path: Path) -> None:
    from cli import paper_live_enablement_switch

    packet = paper_live_enablement_switch.build_switch_packet(
        artifact_dir=tmp_path / "audit",
        env=_ready_live_env(),
        broker_adapter=_CleanLiveBroker(),
        scheduler_state_provider=lambda: {"enabled": False, "source": "test"},
    )

    assert packet["outcome"] == "blocked_with_reasons"
    assert (
        "approved final review decision artifact is required"
        in packet["blocker_register"]["blockers"]
    )


def test_live_enablement_switch_apply_requires_typed_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    from cli import paper_live_enablement_switch

    artifact_dir = tmp_path / "audit"
    review_path = artifact_dir / "paper_live_enablement_final_review_20260622T182100Z.json"
    _write_ready_final_review(review_path)
    _write_json(
        artifact_dir / "paper_live_enablement_final_review_decision_20260622T182200Z.json",
        {
            "artifact_type": "paper_live_enablement_final_review_decision",
            "created_at": "2026-06-22T18:22:00+00:00",
            "outcome": "approve_live_enablement_switch_implementation",
            "artifact_refs": [str(review_path)],
        },
    )
    monkeypatch.setattr(paper_live_enablement_switch, "_timestamp", lambda: "20260622T182300Z")

    blocked = paper_live_enablement_switch.build_switch_packet(
        artifact_dir=artifact_dir,
        env=_ready_live_env(),
        broker_adapter=_CleanLiveBroker(),
        scheduler_state_provider=lambda: {"enabled": False, "source": "test"},
        apply=True,
        confirmation="wrong",
        now=datetime(2026, 6, 22, 18, 23, tzinfo=timezone.utc),
    )
    plan_only = paper_live_enablement_switch.build_switch_packet(
        artifact_dir=artifact_dir,
        env=_ready_live_env(),
        broker_adapter=_CleanLiveBroker(),
        scheduler_state_provider=lambda: {"enabled": False, "source": "test"},
        apply=True,
        confirmation="APPLY LIVE SWITCH",
        now=datetime(2026, 6, 22, 18, 23, tzinfo=timezone.utc),
    )

    assert blocked["outcome"] == "blocked_with_reasons"
    assert (
        "typed confirmation APPLY LIVE SWITCH is required"
        in blocked["blocker_register"]["blockers"]
    )
    assert plan_only["outcome"] == "blocked_with_reasons"
    assert plan_only["live_switch_applied"] is False
    assert plan_only["broker_mutation"] is False
    assert plan_only["runtime_config_mutation"] is False
    assert plan_only["env_var_mutation"] is False
    assert plan_only["scheduler_mutation"] is False
    assert (
        paper_live_enablement_switch.CONTROLLER_UNAVAILABLE_BLOCKER
        in plan_only["blocker_register"]["blockers"]
    )


def test_live_enablement_rollback_writes_proof_packet(tmp_path: Path, monkeypatch) -> None:
    from cli import paper_live_enablement_switch

    monkeypatch.setattr(paper_live_enablement_switch, "_timestamp", lambda: "20260622T183000Z")

    packet = paper_live_enablement_switch.build_rollback_packet(
        artifact_dir=tmp_path / "audit",
        reason="Operator requested rollback after supervised window.",
        apply=True,
        confirmation="ROLLBACK LIVE SWITCH",
        now=datetime(2026, 6, 22, 18, 30, tzinfo=timezone.utc),
    )

    assert packet["artifact_type"] == "paper_live_enablement_rollback"
    assert packet["outcome"] == "blocked_with_reasons"
    assert packet["rollback_applied"] is False
    assert packet["broker_mutation"] is False
    assert packet["runtime_config_mutation"] is False
    assert packet["env_var_mutation"] is False
    assert (
        paper_live_enablement_switch.CONTROLLER_UNAVAILABLE_BLOCKER
        in packet["blocker_register"]["blockers"]
    )
    assert packet["target_execution_mode"] == "paper_broker"
    assert packet["scheduler_mutation"] is False
    assert packet["rollback_artifact"].endswith(
        "paper_live_enablement_rollback_20260622T183000Z.json"
    )


def test_packet_is_not_an_applied_rollback(tmp_path: Path) -> None:
    from cli.paper_live_enablement_switch import build_rollback_packet

    result = build_rollback_packet(
        artifact_dir=tmp_path,
        reason="probe",
        apply=True,
        confirmation="ROLLBACK LIVE SWITCH",
    )

    assert result["rollback_applied"] is False
    assert result["broker_mutation"] is False
    assert result["runtime_config_mutation"] is False
    assert result["env_var_mutation"] is False


def test_rollback_apply_request_still_requires_typed_confirmation(tmp_path: Path) -> None:
    from cli import paper_live_enablement_switch

    result = paper_live_enablement_switch.build_rollback_packet(
        artifact_dir=tmp_path,
        reason="probe",
        apply=True,
        confirmation="wrong",
    )

    assert result["outcome"] == "blocked_with_reasons"
    assert result["rollback_applied"] is False
    assert (
        "typed confirmation ROLLBACK LIVE SWITCH is required"
        in result["blocker_register"]["blockers"]
    )
    assert (
        paper_live_enablement_switch.CONTROLLER_UNAVAILABLE_BLOCKER
        in result["blocker_register"]["blockers"]
    )


class _CleanLiveBroker:
    base_url = "https://api.alpaca.markets"

    def get_account(self) -> Any:
        return {
            "account_id": "live-account-1",
            "status": "ACTIVE",
            "is_paper": False,
            "trading_blocked": False,
        }

    def get_market_clock(self) -> Any:
        return {"is_open": True, "timestamp": "2026-06-22T18:23:00+00:00"}

    def list_open_orders(self, client_order_id_prefix: str | None = None) -> list[Any]:
        return []


def _ready_live_env() -> dict[str, str]:
    return {
        "EXECUTION_MODE": "live",
        "EXECUTION_LIVE_BROKER_ENABLED": "true",
        "EXECUTION_REQUIRE_PAPER_ACCOUNT": "false",
        "EXECUTION_MARKET_HOURS_GUARD": "true",
        "EXECUTION_MAX_ORDER_NOTIONAL": "100",
        "EXECUTION_MAX_ORDER_SHARES": "1",
        "EXECUTION_MAX_SYMBOL_POSITION_SHARES": "1",
        "BREAK_GLASS_ENABLED": "true",
        "ALPACA_LIVE_BASE_URL": "https://api.alpaca.markets",
        "ALPACA_API_KEY_ID": "key",
        "ALPACA_API_SECRET_KEY": "secret",
    }


def _write_ready_final_review(path: Path) -> None:
    _write_json(
        path,
        {
            "artifact_type": "paper_live_enablement_final_review",
            "created_at": "2026-06-22T18:21:00+00:00",
            "outcome": "ready_for_final_enablement_slice",
            "blocker_register": {"blockers": []},
            "live_trading_enabled": False,
            "broker_mutation": False,
            "runtime_config_mutation": False,
            "scheduler_mutation": False,
            "env_var_mutation": False,
        },
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
