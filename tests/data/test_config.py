from __future__ import annotations

import json

import pytest

from data import config as data_config
from data.config import DataProviderConfig, ProviderConfigError


def test_provider_health_probe_defaults_are_applied() -> None:
    config = DataProviderConfig.from_env(
        {
            "ALPHA_VANTAGE_API_KEY": "alpha",
            "FINNHUB_API_KEY": "finn",
            "FRED_API_KEY": "fred",
            "NEWSAPI_KEY": "news",
        }
    )

    assert config.provider_health_ttl_seconds == 300
    assert config.provider_health_probe_symbol == "SPY"
    assert config.provider_health_probe_series_id == "DGS10"
    assert config.provider_health_probe_query == "markets"


def test_provider_health_ttl_must_be_positive_integer() -> None:
    with pytest.raises(ProviderConfigError):
        DataProviderConfig.from_env(
            {
                "ALPHA_VANTAGE_API_KEY": "alpha",
                "FINNHUB_API_KEY": "finn",
                "FRED_API_KEY": "fred",
                "NEWSAPI_KEY": "news",
                "PROVIDER_HEALTH_TTL_SECONDS": "0",
            }
        )


def test_provider_readiness_never_serializes_credentials() -> None:
    payload = data_config.build_provider_readiness(
        required_providers=("alpha_vantage", "finnhub"),
        env={
            "ALPHA_VANTAGE_API_KEY": "alpha-secret-123",
            "FINNHUB_API_KEY": "finnhub-secret-456",
        },
    )

    assert payload == {
        "status": "ready",
        "offline": True,
        "dotenv_loaded": False,
        "network_probes": False,
        "required_providers": ["alpha_vantage", "finnhub"],
        "missing_providers": [],
        "providers": {
            "alpha_vantage": {
                "configured": True,
                "required_environment": ["ALPHA_VANTAGE_API_KEY"],
                "missing_environment": [],
            },
            "finnhub": {
                "configured": True,
                "required_environment": ["FINNHUB_API_KEY"],
                "missing_environment": [],
            },
        },
    }
    serialized = json.dumps(payload)
    assert "alpha-secret-123" not in serialized
    assert "finnhub-secret-456" not in serialized


def test_provider_readiness_fails_closed_for_missing_required_provider() -> None:
    payload = data_config.build_provider_readiness(
        required_providers=("alpha_vantage", "fred"),
        env={"ALPHA_VANTAGE_API_KEY": "configured", "FRED_API_KEY": "   "},
    )

    assert payload["status"] == "blocked"
    assert payload["missing_providers"] == ["fred"]
    assert payload["providers"]["fred"] == {
        "configured": False,
        "required_environment": ["FRED_API_KEY"],
        "missing_environment": ["FRED_API_KEY"],
    }


def test_provider_readiness_rejects_empty_or_unknown_provider_sets() -> None:
    with pytest.raises(ProviderConfigError, match="at least one required provider"):
        data_config.build_provider_readiness(required_providers=(), env={})

    with pytest.raises(ProviderConfigError, match="unsupported provider: unknown"):
        data_config.build_provider_readiness(required_providers=("unknown",), env={})
