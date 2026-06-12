from __future__ import annotations

import pytest

from agents.config import AgentRuntimeConfig


def test_agent_runtime_config_defaults() -> None:
    config = AgentRuntimeConfig.from_env({})
    assert config.tick_interval_seconds == 5.0
    assert config.max_ticks is None
    assert config.concurrency == 1
    assert config.enabled_agents is None
    assert config.runtime_name == "default"
    assert config.runtime_lease_seconds == 30
    assert config.break_glass_enabled is False
    assert config.experimental_strategies is None
    assert config.catalyst_research_input_path is None
    assert config.governance.profile == "dev"


def test_agent_runtime_config_parses_lists_and_limits() -> None:
    config = AgentRuntimeConfig.from_env(
        {
            "AGENT_TICK_INTERVAL": "2.5",
            "AGENT_MAX_TICKS": "10",
            "AGENT_CONCURRENCY": "3",
            "AGENT_ENABLED": "director,quant",
            "AGENT_PIPELINE": "director,quant,risk",
            "RUNTIME_NAME": "runtime-a",
            "RUNTIME_LEASE_SECONDS": "45",
            "BREAK_GLASS_ENABLED": "true",
            "BREAK_GLASS_DEFAULT_TTL_SECONDS": "600",
            "BREAK_GLASS_MAX_TTL_SECONDS": "3600",
            "EXPERIMENTAL_STRATEGIES": "catalyst",
            "CATALYST_RESEARCH_INPUT_PATH": "tests/fixtures/research_inputs/catalyst.json",
            "RUNTIME_PROFILE": "staging",
        }
    )
    assert config.tick_interval_seconds == 2.5
    assert config.max_ticks == 10
    assert config.concurrency == 3
    assert config.enabled_agents == ["director", "quant"]
    assert config.pipeline == ["director", "quant", "risk"]
    assert config.runtime_name == "runtime-a"
    assert config.runtime_lease_seconds == 45
    assert config.break_glass_enabled is True
    assert config.break_glass_default_ttl_seconds == 600
    assert config.break_glass_max_ttl_seconds == 3600
    assert config.experimental_strategies == ["catalyst"]
    assert config.catalyst_research_input_path == "tests/fixtures/research_inputs/catalyst.json"
    assert config.governance.profile == "staging"


def test_agent_runtime_config_rejects_non_positive_values() -> None:
    with pytest.raises(ValueError):
        AgentRuntimeConfig.from_env({"AGENT_TICK_INTERVAL": "0"})
    with pytest.raises(ValueError):
        AgentRuntimeConfig.from_env({"AGENT_CONCURRENCY": "-1"})
    with pytest.raises(ValueError):
        AgentRuntimeConfig.from_env({"BREAK_GLASS_ENABLED": "maybe"})


def test_agent_runtime_config_rejects_unknown_experimental_strategy() -> None:
    with pytest.raises(ValueError, match="EXPERIMENTAL_STRATEGIES"):
        AgentRuntimeConfig.from_env({"EXPERIMENTAL_STRATEGIES": "catalyst,unknown"})
