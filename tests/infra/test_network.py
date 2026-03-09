from __future__ import annotations

import pytest
import requests

from data.cache import TTLCache
from data.providers.base import BaseProvider
from infra.network import NetworkAllowlistPolicy, reset_network_allowlist_policy_cache


class DummyProvider(BaseProvider):
    def ping(self) -> bool:
        return True


def test_allowlist_policy_allows_subdomains() -> None:
    policy = NetworkAllowlistPolicy(enabled=True, enforce=True, domains=("example.com",))
    allowed, reason = policy.validate("https://api.example.com/v1")
    assert allowed is True
    assert reason is None


def test_allowlist_policy_blocks_unknown_domains() -> None:
    policy = NetworkAllowlistPolicy(enabled=True, enforce=True, domains=("example.com",))
    allowed, reason = policy.validate("https://bad.actor.net/path")
    assert allowed is False
    assert reason is not None and reason.startswith("host_not_allowed")


def test_allowlist_policy_uses_profile_defaults() -> None:
    policy = NetworkAllowlistPolicy.from_env({"RUNTIME_PROFILE": "staging"})
    assert policy.enabled is True
    assert policy.enforce is False


def test_requests_patch_blocks_disallowed_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_network_allowlist_policy_cache()
    if hasattr(requests, "_agenthedge_timeout_patched"):
        monkeypatch.delattr(requests, "_agenthedge_timeout_patched", raising=False)
    monkeypatch.setenv("NETWORK_ALLOWLIST_ENABLED", "true")
    monkeypatch.setenv("NETWORK_ALLOWLIST_ENFORCE", "true")
    monkeypatch.setenv("NETWORK_ALLOWLIST_DOMAINS", "allowed.test")

    def fake_request(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        response.url = str(url)
        return response

    monkeypatch.setattr(requests.sessions.Session, "request", fake_request)
    DummyProvider(name="dummy", cache=TTLCache(), http_timeout_seconds=1.0)

    with pytest.raises(PermissionError):
        requests.get("https://blocked.test/resource")
