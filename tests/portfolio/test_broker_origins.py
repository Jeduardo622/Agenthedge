from __future__ import annotations

from typing import Type

import pytest

from portfolio.broker import AlpacaLiveBrokerAdapter, AlpacaPaperBrokerAdapter


@pytest.mark.parametrize(
    "base_url",
    [
        "http://paper-api.alpaca.markets",
        "https://user@paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets:8443",
        "https://paper-api.alpaca.markets.attacker.example",
        "https://attacker-paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets/orders",
        "https://paper-api.alpaca.markets/v20",
        "https://paper-api.alpaca.markets?target=attacker",
        "https://paper-api.alpaca.markets#attacker",
        "https://api.alpaca.markets",
    ],
)
def test_paper_host_is_exact(base_url: str) -> None:
    with pytest.raises(ValueError, match="paper base URL"):
        AlpacaPaperBrokerAdapter(
            api_key_id="fake",
            api_secret_key="fake",
            base_url=base_url,
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.alpaca.markets",
        "https://user:password@api.alpaca.markets",
        "https://api.alpaca.markets:8443",
        "https://api.alpaca.markets.attacker.example",
        "https://attacker-api.alpaca.markets",
        "https://api.alpaca.markets/orders",
        "https://api.alpaca.markets/v20",
        "https://api.alpaca.markets?target=attacker",
        "https://api.alpaca.markets#attacker",
        "https://paper-api.alpaca.markets",
    ],
)
def test_live_host_is_exact(base_url: str) -> None:
    with pytest.raises(ValueError, match="live base URL"):
        AlpacaLiveBrokerAdapter(
            api_key_id="fake",
            api_secret_key="fake",
            base_url=base_url,
        )


@pytest.mark.parametrize(
    ("adapter_type", "base_url", "expected"),
    [
        (
            AlpacaPaperBrokerAdapter,
            "https://paper-api.alpaca.markets/",
            "https://paper-api.alpaca.markets",
        ),
        (
            AlpacaPaperBrokerAdapter,
            "https://paper-api.alpaca.markets/v2/",
            "https://paper-api.alpaca.markets",
        ),
        (
            AlpacaPaperBrokerAdapter,
            "https://paper-api.alpaca.markets:443/v2",
            "https://paper-api.alpaca.markets",
        ),
        (
            AlpacaLiveBrokerAdapter,
            "https://api.alpaca.markets/",
            "https://api.alpaca.markets",
        ),
        (
            AlpacaLiveBrokerAdapter,
            "https://api.alpaca.markets:443/v2/",
            "https://api.alpaca.markets",
        ),
    ],
)
def test_alpaca_origin_normalizes_supported_equivalent_urls(
    adapter_type: Type[AlpacaPaperBrokerAdapter],
    base_url: str,
    expected: str,
) -> None:
    adapter = adapter_type(
        api_key_id="fake",
        api_secret_key="fake",
        base_url=base_url,
    )

    assert adapter.base_url == expected
