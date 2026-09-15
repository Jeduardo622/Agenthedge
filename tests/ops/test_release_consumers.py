"""Synthetic evidence exercises consumers; it does not qualify a broker account."""

from dataclasses import replace
from datetime import timedelta

import pytest

from agents.config import AgentRuntimeConfig, LiveEnablementReadiness
from portfolio.safety import ExecutionSafetyConfig
from tests.ops.test_release_gate import IDENTITY, KEY, NOW, dossier, sign


def live_env():
    return {
        "EXECUTION_MODE": "live",
        "EXECUTION_LIVE_BROKER_ENABLED": "true",
        "EXECUTION_MAX_ORDER_NOTIONAL": "100",
        "EXECUTION_MAX_ORDER_SHARES": "1",
        "EXECUTION_MAX_SYMBOL_POSITION_SHARES": "1",
        "LIVE_ENABLEMENT_3_SESSION_STABILITY_CONFIRMED": "true",
        "LIVE_ENABLEMENT_LIVE_CREDENTIALS_VERIFIED": "true",
        "LIVE_ENABLEMENT_RISK_CAPS_APPROVED": "true",
    }


def qualified():
    from ops.release_gate import ReleaseTrust

    config = replace(
        AgentRuntimeConfig.from_env({}),
        execution_mode="live",
        live_enablement_readiness=LiveEnablementReadiness(True, True, True),
        execution_safety=ExecutionSafetyConfig(100.0, 1.0, 1.0),
    )
    identity = replace(IDENTITY, config_hash=config.release_config_hash())
    trust = ReleaseTrust(identity, {"test-reviewer": KEY}, "paper-owner")
    return trust, sign(dossier(identity=identity))


def test_legacy_boolean_readiness_is_not_live_authorization():
    with pytest.raises(ValueError, match="release evidence"):
        AgentRuntimeConfig.from_env(live_env())


def test_signed_live_config_binds_actual_caps_and_expires():
    trust, evidence = qualified()
    config = AgentRuntimeConfig.from_env(
        live_env(), release_trust=trust, release_evidence=evidence, now=NOW
    )
    assert config.execution_mode == "live"
    assert config.execution_safety.max_order_notional == 100
    with pytest.raises(ValueError, match="config_hash"):
        AgentRuntimeConfig.from_env(
            {**live_env(), "EXECUTION_MAX_ORDER_NOTIONAL": "101"},
            release_trust=trust,
            release_evidence=evidence,
            now=NOW,
        )
    with pytest.raises(ValueError, match="release evidence"):
        AgentRuntimeConfig.from_env(
            live_env(),
            release_trust=trust,
            release_evidence=evidence,
            now=NOW + timedelta(minutes=6),
        )


def test_trust_is_independent_copied_and_not_in_repr_or_output():
    from ops.release_gate import ReleaseTrust, release_decision

    keys = {"test-reviewer": KEY}
    trust = ReleaseTrust(IDENTITY, keys, "paper-owner")
    keys.clear()
    assert release_decision(sign(dossier()), trust=trust, stage="live_start", now=NOW)["passed"]
    assert KEY.decode() not in repr(trust)
    assert not release_decision(
        {**sign(dossier()), "trusted_keys": {"test-reviewer": KEY.decode()}},
        trust=None,
        stage="live_start",
        now=NOW,
    )["passed"]


@pytest.mark.parametrize("damage", ["none", "signature", "stale", "identity"])
def test_report_consumers_share_the_same_release_decision(tmp_path, damage):
    from cli.paper_live_readiness_report import build_live_readiness_report
    from cli.paper_review_board import build_review_board
    from ops.release_gate import release_decision

    trust, evidence = qualified()
    current = NOW
    if damage == "signature":
        evidence["signature"]["digest"] = "0" * 64
    elif damage == "stale":
        current += timedelta(minutes=6)
    elif damage == "identity":
        trust = replace(trust, expected=replace(trust.expected, account_id="other"))
    expected = release_decision(evidence, trust=trust, stage="live_start", now=current)
    for builder in (build_review_board, build_live_readiness_report):
        report = builder(
            artifact_dir=tmp_path / builder.__name__,
            now=current,
            release_trust=trust,
            release_evidence=evidence,
            release_stage="live_start",
        )
        assert report["release_gate"] == expected
        assert KEY.decode() not in str(report)
        assert str(expected["passed"]) in report["markdown"]


def test_switch_checks_release_and_actual_account_and_configuration(tmp_path):
    from cli.paper_live_enablement_switch import build_switch_packet
    from ops.release_gate import ReleaseTrust, release_decision
    from tests.cli.test_paper_live_enablement_switch import (
        _CleanLiveBroker,
        _ready_live_env,
        _write_json,
        _write_ready_final_review,
    )

    env = {**live_env(), **_ready_live_env()}
    config = replace(
        AgentRuntimeConfig.from_env({}),
        execution_mode="live",
        break_glass_enabled=True,
        live_enablement_readiness=LiveEnablementReadiness(True, True, True),
        execution_safety=ExecutionSafetyConfig(100.0, 1.0, 1.0, True, False),
    )
    identity = replace(
        IDENTITY, account_id="live-account-1", config_hash=config.release_config_hash()
    )
    trust = ReleaseTrust(identity, {"test-reviewer": KEY}, "paper-owner")
    evidence = sign(dossier(identity=identity))
    review = tmp_path / "paper_live_enablement_final_review_20260915.json"
    _write_ready_final_review(review)
    _write_json(
        tmp_path / "paper_live_enablement_final_review_decision_20260915.json",
        {
            "artifact_type": "paper_live_enablement_final_review_decision",
            "created_at": NOW.isoformat(),
            "outcome": "approve_live_enablement_switch_implementation",
            "artifact_refs": [str(review)],
        },
    )
    args = dict(
        artifact_dir=tmp_path,
        env=env,
        broker_adapter=_CleanLiveBroker(),
        scheduler_state_provider=lambda: {"enabled": False},
        now=NOW,
        release_trust=trust,
        release_evidence=evidence,
    )
    packet = build_switch_packet(**args)
    assert packet["release_gate"] == release_decision(
        evidence, trust=trust, stage="live_start", now=NOW
    )
    assert packet["outcome"] == "ready_to_apply_live_switch"
    assert not packet["live_switch_applied"]
    assert KEY.decode() not in str(packet)
    changed = build_switch_packet(**{**args, "env": {**env, "EXECUTION_MAX_ORDER_NOTIONAL": "101"}})
    assert changed["outcome"] == "blocked_with_reasons"
    assert any("config_hash" in reason for reason in changed["blocker_register"]["blockers"])
    wrong_account = replace(trust, expected=replace(identity, account_id="other"))
    changed = build_switch_packet(**{**args, "release_trust": wrong_account})
    assert changed["outcome"] == "blocked_with_reasons"
    assert not changed["release_gate"]["passed"]
    applied = build_switch_packet(**{**args, "apply": True, "confirmation": "APPLY LIVE SWITCH"})
    assert not applied["live_switch_applied"]
    assert applied["outcome"] == "blocked_with_reasons"
