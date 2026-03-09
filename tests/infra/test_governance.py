from __future__ import annotations

import pytest

from infra.governance import RuntimeGovernanceConfig


def test_governance_defaults_by_profile() -> None:
    dev = RuntimeGovernanceConfig.from_env({"RUNTIME_PROFILE": "dev"})
    staging = RuntimeGovernanceConfig.from_env({"RUNTIME_PROFILE": "staging"})
    prod = RuntimeGovernanceConfig.from_env({"RUNTIME_PROFILE": "prod"})

    assert dev.bus_acl_enforce is False
    assert staging.bus_acl_enforce is False
    assert prod.bus_acl_enforce is True
    assert staging.network_allowlist_enabled is True
    assert staging.network_allowlist_enforce is False
    assert prod.network_allowlist_enforce is True


def test_governance_parses_overrides_and_redacts_domains() -> None:
    cfg = RuntimeGovernanceConfig.from_env(
        {
            "RUNTIME_PROFILE": "staging",
            "BUS_ACL_ENFORCE": "true",
            "NETWORK_ALLOWLIST_ENABLED": "true",
            "NETWORK_ALLOWLIST_ENFORCE": "false",
            "NETWORK_ALLOWLIST_DOMAINS": "api.test,news.test",
            "RUNTIME_EVENT_LAG_ALERT_THRESHOLD": "75",
            "RUNTIME_DELIVERY_RETRY_RATE_ALERT_THRESHOLD": "0.5",
        }
    )
    summary = cfg.redacted_summary()

    assert cfg.bus_acl_enforce is True
    assert cfg.network_allowlist_domains == ("api.test", "news.test")
    assert summary["network_allowlist_domain_count"] == 2
    assert summary["runtime_event_lag_alert_threshold"] == 75.0
    assert summary["runtime_delivery_retry_rate_alert_threshold"] == 0.5


def test_governance_rejects_invalid_failure_action() -> None:
    with pytest.raises(ValueError):
        RuntimeGovernanceConfig.from_env(
            {
                "RUNTIME_PROFILE": "staging",
                "RUNTIME_AGENT_FAILURE_ACTION": "panic",
            }
        )
