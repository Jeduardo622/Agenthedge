"""SIP preflight evidence uses only synthetic credentials and transport."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from ops.paper_preflight import ClockLimits, capture_preflight, verify_preflight
from tests.ops.test_paper_preflight import ENV, NOW, TRUST, Response, payloads


def capture_sip(values=None, denied=None):
    from data.sip import SipQuotePolicy

    calls = []
    responses = iter(payloads() if values is None else values)

    def get(url, **options):
        calls.append((url, options))
        response = Response(next(responses))
        if denied is not None and "data.alpaca.markets" in url:
            response.status_code = denied
        return response

    envelope = capture_preflight(
        trust=TRUST,
        issuer="test-issuer",
        environment=ENV,
        symbols=("SPY",),
        quote_policy=SipQuotePolicy(Decimal(5), Decimal("0.001")),
        limits=ClockLimits(1, 2),
        get=get,
        now=lambda: NOW,
        monotonic=lambda: 0,
    )
    return envelope, calls


def test_sip_preflight_signs_actual_feed_and_exact_four_gets():
    envelope, calls = capture_sip()
    assert envelope["payload"]["passed"] is True
    assert envelope["payload"]["details"]["feed"] == "sip"
    assert envelope["payload"]["details"]["checks"]["feed:SPY"]["feed"] == "sip"
    assert len(calls) == 4
    assert all(options["params"] == {"feed": "sip"} for _, options in calls[2:])
    assert all(
        options["timeout"] == 2 and options["allow_redirects"] is False for _, options in calls
    )
    assert verify_preflight(envelope, trust=TRUST, now=NOW)
    assert envelope["payload"]["authorizes_activation"] is False
    assert "synthetic-secret" not in json.dumps(envelope)


@pytest.mark.parametrize("kind,index", [("quote", 2), ("trade", 3)])
@pytest.mark.parametrize("age,passed", [(5, True), (5.000001, False), (-0.000001, False)])
def test_sip_preflight_preserves_both_timestamp_boundaries(kind, index, age, passed):
    values = payloads()
    values[index][kind]["t"] = (NOW - timedelta(seconds=age)).isoformat()
    envelope, calls = capture_sip(values)
    assert envelope["payload"]["passed"] is passed
    assert len(calls) == 4
    assert verify_preflight(envelope, trust=TRUST, now=NOW)


def test_sip_denial_is_signed_hold_without_retry_or_feed_substitution():
    envelope, calls = capture_sip(denied=403)
    assert envelope["payload"]["passed"] is False
    assert envelope["payload"]["blockers"] == ["feed:SPY:unavailable_or_invalid"]
    assert len(calls) == 3
    assert calls[-1][1]["params"] == {"feed": "sip"}
    assert verify_preflight(envelope, trust=TRUST, now=NOW)
