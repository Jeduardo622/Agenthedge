from __future__ import annotations

import pytest

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
