from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from data.snapshot import (
    CanonicalQuote,
    CanonicalSnapshot,
    ResearchObservation,
    snapshot_to_mapping,
)

UTC = timezone.utc
EVENT_AT = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
AVAILABLE_AT = EVENT_AT + timedelta(seconds=1)
RECEIVED_AT = AVAILABLE_AT + timedelta(seconds=1)


def test_canonical_snapshot_exposes_exact_quote_contract() -> None:
    quote = CanonicalQuote(
        last=Decimal("100.25"),
        previous_close=Decimal("99.50"),
        bid=Decimal("100.20"),
        ask=Decimal("100.30"),
        volume=Decimal("1250"),
    )
    snapshot = _snapshot(quote=quote)

    assert snapshot.price == Decimal("100.25")
    assert snapshot.quote.previous_close == Decimal("99.50")
    assert snapshot.quote.bid == Decimal("100.20")
    assert snapshot.quote.ask == Decimal("100.30")
    assert snapshot.quote.volume == Decimal("1250")


@pytest.mark.parametrize(
    "value", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")]
)
@pytest.mark.parametrize("field", ["last", "previous_close"])
def test_quote_rejects_non_positive_or_non_finite_required_prices(
    field: str, value: Decimal
) -> None:
    values = {"last": Decimal("100"), "previous_close": Decimal("99")}
    values[field] = value

    with pytest.raises(ValueError, match=field):
        CanonicalQuote(**values)


@pytest.mark.parametrize("field", ["bid", "ask"])
@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_quote_rejects_invalid_optional_prices(field: str, value: Decimal) -> None:
    values: dict[str, Decimal | None] = {
        "last": Decimal("100"),
        "previous_close": Decimal("99"),
        "bid": None,
        "ask": None,
        "volume": None,
    }
    values[field] = value

    with pytest.raises(ValueError, match=field):
        CanonicalQuote(**values)


@pytest.mark.parametrize("value", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_quote_rejects_invalid_volume(value: Decimal) -> None:
    with pytest.raises(ValueError, match="volume"):
        _quote(volume=value)


def test_quote_rejects_crossed_bid_and_ask() -> None:
    with pytest.raises(ValueError, match="bid must not exceed ask"):
        _quote(bid=Decimal("101"), ask=Decimal("100"))


@pytest.mark.parametrize("field", ["event_at", "available_at", "received_at"])
def test_snapshot_requires_utc_aware_times(field: str) -> None:
    values = _snapshot_values()
    values[field] = datetime(2026, 9, 14, 15, 0)

    with pytest.raises(ValueError, match=field):
        CanonicalSnapshot(**values)


def test_snapshot_rejects_non_causal_time_order() -> None:
    with pytest.raises(ValueError, match="available_at"):
        _snapshot(available_at=EVENT_AT - timedelta(seconds=1))

    with pytest.raises(ValueError, match="received_at"):
        _snapshot(received_at=AVAILABLE_AT - timedelta(seconds=1))


@pytest.mark.parametrize("field", ["symbol", "source", "revision", "checksum"])
def test_snapshot_requires_identity_and_provenance(field: str) -> None:
    values = _snapshot_values()
    values[field] = "  "

    with pytest.raises(ValueError, match=field):
        CanonicalSnapshot(**values)


def test_research_observation_is_immutable_validated_and_individually_visible() -> None:
    observation = _research(available_at=AVAILABLE_AT)

    assert observation.is_visible_at(AVAILABLE_AT) is True
    assert observation.is_visible_at(EVENT_AT) is False
    with pytest.raises(FrozenInstanceError):
        observation.source = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="event_at"):
        _research(event_at=datetime(2026, 9, 14, 15, 0))
    with pytest.raises(ValueError, match="available_at"):
        _research(available_at=EVENT_AT - timedelta(seconds=1))


@pytest.mark.parametrize("field", ["source", "revision", "checksum"])
def test_research_observation_requires_provenance(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _research(**{field: "  "})


def test_research_value_is_defensively_immutable() -> None:
    original = {"calendar": [{"date": "2026-10-01", "kind": "earnings"}]}
    observation = _research(value=original)

    original["calendar"][0]["date"] = "2099-01-01"

    calendar = observation.value["calendar"]  # type: ignore[index]
    assert calendar[0]["date"] == "2026-10-01"
    with pytest.raises(TypeError):
        observation.value["new"] = "mutation"  # type: ignore[index]


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
@pytest.mark.parametrize("nested", [False, True])
def test_research_value_rejects_non_finite_numbers(value: object, nested: bool) -> None:
    candidate = {"nested": [value]} if nested else value

    with pytest.raises(ValueError, match="research value must be finite"):
        _research(value=candidate)


@pytest.mark.parametrize("value", [0.0, -1.25, Decimal("0"), Decimal("-1.25")])
def test_research_value_preserves_finite_numeric_scalars(value: object) -> None:
    assert _research(value=value).value == value


def test_snapshot_rejects_research_not_individually_visible_at_availability() -> None:
    visible = _research(value="visible")
    future = _research(value="future", available_at=AVAILABLE_AT + timedelta(seconds=1))

    assert _snapshot(fundamentals={"visible": visible}, news=(visible,)).fundamentals == {
        "visible": visible
    }
    with pytest.raises(ValueError, match="fundamentals.future"):
        _snapshot(fundamentals={"future": future})
    with pytest.raises(ValueError, match=r"news\[0\]"):
        _snapshot(news=(future,))


def test_snapshot_and_nested_research_collections_are_immutable() -> None:
    observation = _research()
    snapshot = _snapshot(fundamentals={"eps": observation}, news=(observation,))

    with pytest.raises(FrozenInstanceError):
        snapshot.symbol = "MSFT"  # type: ignore[misc]
    with pytest.raises(TypeError):
        snapshot.fundamentals["new"] = observation  # type: ignore[index]
    assert isinstance(snapshot.news, tuple)


def _quote(**overrides: Decimal | None) -> CanonicalQuote:
    values: dict[str, Decimal | None] = {
        "last": Decimal("100"),
        "previous_close": Decimal("99"),
        "bid": None,
        "ask": None,
        "volume": None,
    }
    values.update(overrides)
    return CanonicalQuote(**values)


def _research(**overrides: object) -> ResearchObservation:
    values: dict[str, object] = {
        "value": "observed",
        "event_at": EVENT_AT,
        "available_at": AVAILABLE_AT,
        "source": "test-provider",
        "revision": "v1",
        "checksum": "sha256:research",
    }
    values.update(overrides)
    return ResearchObservation(**values)


def _snapshot(**overrides: object) -> CanonicalSnapshot:
    values = _snapshot_values()
    values.update(overrides)
    return CanonicalSnapshot(**values)


def _snapshot_values() -> dict[str, object]:
    return {
        "symbol": "SPY",
        "event_at": EVENT_AT,
        "available_at": AVAILABLE_AT,
        "received_at": RECEIVED_AT,
        "quote": _quote(),
        "source": "test-provider",
        "revision": "v1",
        "checksum": "sha256:snapshot",
        "fundamentals": {},
        "news": (),
    }


@pytest.mark.parametrize("value", [Decimal("1.2"), b"bytes", {("tuple",): "key"}])
def test_snapshot_serialization_rejects_non_json_research(value: object) -> None:
    observation = _research(value=value)
    with pytest.raises(TypeError, match="JSON"):
        snapshot_to_mapping(_snapshot(news=(observation,)))


def test_snapshot_serialization_is_json_safe_and_preserves_provenance() -> None:
    observation = _research(value={"score": 1.5, "tags": ["earnings"]})
    serialized = snapshot_to_mapping(_snapshot(news=(observation,)))
    assert json.loads(json.dumps(serialized))["news"][0] == {
        "value": {"score": 1.5, "tags": ["earnings"]},
        "event_at": EVENT_AT.isoformat(),
        "available_at": AVAILABLE_AT.isoformat(),
        "source": "test-provider",
        "revision": "v1",
        "checksum": "sha256:research",
    }
