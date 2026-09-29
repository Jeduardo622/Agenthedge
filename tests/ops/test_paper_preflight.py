"""Synthetic observations only: no environment secrets or real provider access."""

import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from data.iex import IexQuotePolicy
from ops.paper_preflight import (
    DATA_URL,
    PAPER_URL,
    ClockLimits,
    capture_preflight,
    verify_preflight,
    write_preflight,
)
from ops.release_gate import ReleaseIdentity, ReleaseTrust, release_decision

NOW = datetime(2026, 9, 28, 14, tzinfo=timezone.utc)
OPEN = datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc)
KEY = b"synthetic-test-issuer-key-only-000000000"
TRUST = ReleaseTrust(
    ReleaseIdentity("a" * 40, "synthetic-paper", "paper_broker", *("b" * 64 for _ in range(4))),
    {"test-issuer": KEY},
    "synthetic-paper",
)
ENV = {
    "EXECUTION_MODE": "paper_broker",
    "ALPACA_PAPER_BASE_URL": PAPER_URL,
    "ALPACA_API_KEY_ID": "synthetic-id",
    "ALPACA_API_SECRET_KEY": "synthetic-secret",
}
POLICY = IexQuotePolicy(Decimal(5), Decimal("0.001"))


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def payloads():
    return [
        {
            "id": "synthetic-paper",
            "status": "ACTIVE",
            "trading_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
        },
        {"timestamp": NOW.isoformat(), "is_open": True, "next_open": OPEN.isoformat()},
        {"symbol": "SPY", "quote": {"bp": 500, "ap": 500.1, "t": NOW.isoformat()}},
        {"symbol": "SPY", "trade": {"p": 500, "t": NOW.isoformat()}},
    ]


def capture(*, values=None, env=None, now=None, mono=None, get=None, **kwargs):
    calls = []
    responses = iter(payloads() if values is None else values)

    def transport(url, **options):
        calls.append((url, options))
        return Response(next(responses))

    envelope = capture_preflight(
        trust=kwargs.pop("trust", TRUST),
        issuer="test-issuer",
        environment=ENV if env is None else env,
        symbols=("SPY",),
        quote_policy=POLICY,
        limits=kwargs.pop("limits", ClockLimits(1, 1)),
        get=get or transport,
        now=now or (lambda: NOW),
        monotonic=mono or (lambda: 0),
        **kwargs,
    )
    return envelope, calls


def test_authenticated_gets_capture_signed_read_only_evidence(tmp_path):
    envelope, calls = capture()
    payload = envelope["payload"]
    assert payload["passed"] is True
    assert payload["authorizes_activation"] is False
    assert [url for url, _ in calls] == [
        f"{PAPER_URL}/v2/account",
        f"{PAPER_URL}/v2/clock",
        f"{DATA_URL}/v2/stocks/SPY/quotes/latest",
        f"{DATA_URL}/v2/stocks/SPY/trades/latest",
    ]
    for _, options in calls:
        assert options["allow_redirects"] is False
        assert options["timeout"] == 1
        assert options["headers"]["APCA-API-SECRET-KEY"] == "synthetic-secret"
    assert calls[2][1]["params"] == calls[3][1]["params"] == {"feed": "iex"}
    assert payload["details"]["checks"]["future_market_open"]["timestamp"] == OPEN.isoformat()
    assert verify_preflight(envelope, trust=TRUST, now=NOW)
    assert not release_decision(envelope, trust=TRUST, stage="paper_start", now=NOW)["passed"]
    path = write_preflight(tmp_path, envelope)
    assert json.loads(path.read_bytes()) == envelope
    with pytest.raises(FileExistsError):
        write_preflight(tmp_path, envelope)
    assert KEY.decode() not in path.read_text()
    assert "synthetic-secret" not in path.read_text()


@pytest.mark.parametrize(
    "key,value",
    [
        ("ALPACA_API_KEY_ID", ""),
        ("ALPACA_API_SECRET_KEY", ""),
        ("ALPACA_PAPER_BASE_URL", "https://api.alpaca.markets"),
        ("EXECUTION_MODE", "live"),
    ],
)
def test_invalid_environment_is_signed_hold_without_network(key, value):
    envelope, calls = capture(env={**ENV, key: value})
    assert not calls
    assert envelope["payload"]["blockers"] == ["credentials:unavailable_or_invalid"]
    assert verify_preflight(envelope, trust=TRUST, now=NOW)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p[0].update(id="another-account"),
        lambda p: p[0].update(status="DISABLED"),
        lambda p: p[0].update(account_blocked=True),
        lambda p: p[0].update(trade_suspended_by_user=True),
        lambda p: p[0].update(trade_suspended_by_user="false"),
        lambda p: p[0].pop("trade_suspended_by_user"),
        lambda p: p[0].pop("trading_blocked"),
        lambda p: p[1].update(timestamp="2026-09-28T14:00:00"),
        lambda p: p[1].update(timestamp=(NOW + timedelta(seconds=10)).isoformat()),
        lambda p: p[1].update(next_open=NOW.isoformat()),
        lambda p: p[1].update(next_open=(OPEN + timedelta(days=1)).isoformat()),
        lambda p: p[1].update(next_open=(OPEN + timedelta(minutes=1)).isoformat()),
        lambda p: p[1].update(is_open="true"),
        lambda p: p[2].update(symbol="QQQ"),
        lambda p: p[2]["quote"].update(t=(NOW - timedelta(seconds=6)).isoformat()),
        lambda p: p[3]["trade"].update(t=(NOW - timedelta(seconds=6)).isoformat()),
        lambda p: p[2]["quote"].update(t=(NOW + timedelta(seconds=1)).isoformat()),
        lambda p: p[3]["trade"].update(t=(NOW + timedelta(seconds=1)).isoformat()),
        lambda p: p[2]["quote"].update(bp=501),
        lambda p: p[2]["quote"].update(ap=510),
        lambda p: p[3]["trade"].update(p=0),
        lambda p: p[2]["quote"].update(bp=float("nan")),
        lambda p: p[2]["quote"].update(ap="Infinity"),
    ],
)
def test_bad_observations_never_pass(mutate):
    values = payloads()
    mutate(values)
    envelope, _ = capture(values=values)
    assert envelope["payload"]["passed"] is False
    assert envelope["payload"]["blockers"]
    assert verify_preflight(envelope, trust=TRUST, now=NOW)


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_http_failure_is_redacted_hold(status):
    def get(*args, **kwargs):
        response = Response({"secret": "sensitive-payload"})
        response.status_code = status
        return response

    envelope, _ = capture(get=get)
    assert not envelope["payload"]["passed"]
    assert "sensitive-payload" not in json.dumps(envelope)


def test_timeout_message_and_payload_are_never_leaked():
    def get(*args, **kwargs):
        raise TimeoutError("url?api_key=synthetic-secret")

    envelope, _ = capture(get=get)
    assert not envelope["payload"]["passed"]
    assert "synthetic-secret" not in json.dumps(envelope)


def test_clock_skew_measures_midpoint_and_uncertainty():
    times = iter(
        [
            NOW,
            NOW,
            NOW + timedelta(seconds=0.2),
            NOW + timedelta(seconds=0.2),
            NOW + timedelta(seconds=0.2),
        ]
    )
    monotonic = iter([0, 0, 0.2, 0.2])
    envelope, _ = capture(now=lambda: next(times), mono=lambda: next(monotonic))
    clock = envelope["payload"]["details"]["checks"]["clock"]
    assert clock["server_minus_host_seconds"] == pytest.approx(-0.1)
    assert clock["round_trip_seconds"] == pytest.approx(0.2)
    assert clock["absolute_skew_upper_bound_seconds"] == pytest.approx(0.2)
    assert envelope["payload"]["passed"]


@pytest.mark.parametrize(
    "wall,rtt", [(0.2, 2), (1, 0.2), (-1, 0.2), (0.2, -1), (0.2, float("nan"))]
)
def test_clock_discontinuities_or_bad_rtt_fail(wall, rtt):
    times = iter([NOW, NOW, NOW + timedelta(seconds=wall), NOW + timedelta(seconds=wall)])
    monotonic = iter([0, 0, rtt, rtt])
    envelope, _ = capture(now=lambda: next(times), mono=lambda: next(monotonic))
    assert not envelope["payload"]["passed"]


def test_uncertainty_counts_against_clock_budget():
    times = iter([NOW, NOW, NOW + timedelta(seconds=0.8), NOW + timedelta(seconds=0.8)])
    monotonic = iter([0, 0, 0.8, 0.8])
    envelope, _ = capture(
        now=lambda: next(times), mono=lambda: next(monotonic), limits=ClockLimits(0.5, 1)
    )
    assert not envelope["payload"]["passed"]


def test_calendar_unavailable_fails_closed():
    class Calendar:
        def session_bounds(self, day):
            raise RuntimeError("unavailable")

    envelope, _ = capture(calendar=Calendar())
    assert envelope["payload"]["blockers"] == ["future_market_open:unavailable_or_invalid"]


@pytest.mark.parametrize(
    "start,opening",
    [
        ("2026-07-02T20:00:00+00:00", "2026-07-06T13:30:00+00:00"),
        ("2026-10-30T20:00:00+00:00", "2026-11-02T14:30:00+00:00"),
        ("2026-11-27T18:00:00+00:00", "2026-11-30T14:30:00+00:00"),
    ],
)
def test_real_calendar_holidays_dst_and_weekends(start, opening):
    current = datetime.fromisoformat(start)
    values = payloads()
    values[1].update(timestamp=start, next_open=opening, is_open=False)
    values[2]["quote"]["t"] = values[3]["trade"]["t"] = start
    envelope, _ = capture(values=values, now=lambda: current)
    assert envelope["payload"]["passed"]


def test_capture_completion_rechecks_feed_freshness():
    times = iter([NOW, NOW, NOW, NOW, NOW + timedelta(seconds=6)])
    monotonic = iter([0, 0, 0, 6])
    envelope, _ = capture(now=lambda: next(times), mono=lambda: next(monotonic))
    assert envelope["payload"]["blockers"] == ["feed:expired_during_capture"]


def test_successful_packet_expires_when_market_open_arrives():
    current = OPEN - timedelta(seconds=1)
    values = payloads()
    values[1].update(timestamp=current.isoformat(), is_open=False)
    values[2]["quote"]["t"] = values[3]["trade"]["t"] = current.isoformat()
    envelope, _ = capture(values=values, now=lambda: current)
    assert envelope["payload"]["passed"]
    assert verify_preflight(envelope, trust=TRUST, now=current)
    assert not verify_preflight(envelope, trust=TRUST, now=OPEN)


def test_signature_tampering_wrong_identity_missing_trust_and_expiry():
    envelope, _ = capture()
    altered = copy.deepcopy(envelope)
    altered["payload"]["passed"] = False
    assert not verify_preflight(altered, trust=TRUST, now=NOW)
    for trust in (
        replace(TRUST, trusted_keys={}),
        replace(TRUST, expected=replace(TRUST.expected, sha="c" * 40)),
    ):
        assert not verify_preflight(envelope, trust=trust, now=NOW)
    assert not verify_preflight(envelope, trust=TRUST, now=NOW - timedelta(seconds=1))
    assert not verify_preflight(envelope, trust=TRUST, now=NOW + timedelta(seconds=301))


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, 61])
def test_clock_limits_must_be_explicit_finite_and_bounded(bad):
    with pytest.raises(ValueError):
        ClockLimits(bad, 1)


def test_durable_write_failure_never_returns_success(tmp_path, monkeypatch):
    import ops.paper_preflight as preflight

    envelope, _ = capture()

    def fail(fd):
        raise OSError("disk sync failed")

    monkeypatch.setattr(preflight.os, "fsync", fail)
    with pytest.raises(OSError, match="disk sync failed"):
        write_preflight(tmp_path, envelope)


def test_corrupt_readback_never_returns_success(tmp_path, monkeypatch):
    from pathlib import Path

    envelope, _ = capture()
    monkeypatch.setattr(Path, "read_bytes", lambda path: b"corrupted")
    with pytest.raises(OSError, match="readback failed"):
        write_preflight(tmp_path, envelope)
