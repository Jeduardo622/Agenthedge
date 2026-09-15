import json
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from risk.policy import EtfSectorMap, RiskPolicy


def test_policy_defaults_are_explicit_and_hashed() -> None:
    policy = RiskPolicy.from_mapping({})

    assert policy.max_single_name_fraction == D("0.10")
    assert policy.max_sector_fraction == D("0.25")
    assert policy.max_gross_leverage == D("1.0")
    assert policy.session_loss_pause_fraction == D("0.02")
    assert policy.hard_halt_loss_fraction == D("0.05")
    assert policy.max_order_volume_fraction == D("0.20")
    assert policy.max_slippage_fraction == D("0.005")
    assert policy.allow_short is False
    assert policy.allow_margin is False
    assert len(policy.content_hash) == 64
    assert policy.content_hash == RiskPolicy.from_mapping({}).content_hash
    assert (
        policy.content_hash
        == RiskPolicy.from_mapping({"max_single_name_fraction": "0.100"}).content_hash
    )
    assert (
        policy.content_hash
        != RiskPolicy.from_mapping({"max_single_name_fraction": "0.09"}).content_hash
    )


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "-0.1"])
def test_policy_rejects_nonfinite_and_negative_limits(value: str) -> None:
    with pytest.raises(ValueError):
        RiskPolicy.from_mapping({"max_single_name_fraction": value})


def test_policy_rejects_unknown_or_ambiguous_values() -> None:
    with pytest.raises(ValueError, match="unknown policy fields"):
        RiskPolicy.from_mapping({"position_limit": "0.1"})
    with pytest.raises(ValueError, match="hard_halt"):
        RiskPolicy.from_mapping(
            {"session_loss_pause_fraction": "0.05", "hard_halt_loss_fraction": "0.02"}
        )
    with pytest.raises(ValueError, match="allow_short"):
        RiskPolicy.from_mapping({"allow_short": True})
    with pytest.raises(ValueError, match="max_gross_leverage"):
        RiskPolicy.from_mapping({"max_gross_leverage": "2"})
    with pytest.raises(ValueError, match="etf_sector_map_max_age_days"):
        RiskPolicy.from_mapping({"etf_sector_map_max_age_days": True})


def test_available_etf_sector_map_requires_dated_provenance_and_weights() -> None:
    mapping = EtfSectorMap.from_mapping(
        {
            "schema_version": 1,
            "status": "available",
            "source": "licensed-test-fixture",
            "as_of": "2026-09-14",
            "checksum": "sha256:fixture",
            "funds": {"SYNTH": {"technology": "0.6", "healthcare": "0.4"}},
        }
    )

    assert mapping.available is True
    assert mapping.as_of == date(2026, 9, 14)
    assert mapping.funds["SYNTH"]["technology"] == D("0.6")
    assert mapping.weights_for("synth", on_date=date(2026, 9, 15), max_age_days=30) == {
        "technology": D("0.6"),
        "healthcare": D("0.4"),
    }
    with pytest.raises(TypeError):
        mapping.funds["SYNTH"]["technology"] = D("1")  # type: ignore[index]


@pytest.mark.parametrize(
    "override",
    [
        {"source": ""},
        {"as_of": None},
        {"checksum": ""},
        {"funds": {"SYNTH": {"technology": "0.7", "healthcare": "0.4"}}},
        {"funds": {"SYNTH": {"technology": "NaN", "healthcare": "NaN"}}},
    ],
)
def test_available_etf_sector_map_fails_closed(override: dict[str, object]) -> None:
    values: dict[str, object] = {
        "schema_version": 1,
        "status": "available",
        "source": "licensed-test-fixture",
        "as_of": "2026-09-14",
        "checksum": "sha256:fixture",
        "funds": {"SYNTH": {"technology": "0.6", "healthcare": "0.4"}},
    }
    values.update(override)
    with pytest.raises(ValueError):
        EtfSectorMap.from_mapping(values)


def test_unavailable_etf_map_contains_no_fabricated_weights() -> None:
    mapping = EtfSectorMap.from_mapping(
        {
            "schema_version": 1,
            "status": "unavailable",
            "source": None,
            "as_of": None,
            "checksum": None,
            "funds": {},
        }
    )
    assert mapping.available is False
    assert mapping.funds == {}
    with pytest.raises(ValueError, match="unavailable"):
        mapping.weights_for("SPY", on_date=date(2026, 9, 14), max_age_days=90)


def test_deployed_etf_map_is_explicitly_unavailable() -> None:
    path = Path(__file__).parents[2] / "config" / "risk" / "etf-sector-map.json"
    mapping = EtfSectorMap.from_mapping(json.loads(path.read_text(encoding="utf-8")))

    assert mapping.available is False
    assert mapping.funds == {}


def test_etf_sector_lookup_rejects_missing_and_stale_data() -> None:
    mapping = EtfSectorMap.from_mapping(
        {
            "schema_version": 1,
            "status": "available",
            "source": "licensed-test-fixture",
            "as_of": "2026-01-01",
            "checksum": "sha256:fixture",
            "funds": {"SYNTH": {"technology": "1"}},
        }
    )
    with pytest.raises(ValueError, match="missing"):
        mapping.weights_for("OTHER", on_date=date(2026, 1, 2), max_age_days=30)
    with pytest.raises(ValueError, match="stale"):
        mapping.weights_for("SYNTH", on_date=date(2026, 4, 2), max_age_days=30)
    with pytest.raises(ValueError, match="max_age_days"):
        mapping.weights_for("SYNTH", on_date=date(2026, 1, 2), max_age_days=True)


def test_etf_sector_map_rejects_datetime_as_of() -> None:
    with pytest.raises(ValueError, match="plain ISO date"):
        EtfSectorMap.from_mapping(
            {
                "schema_version": 1,
                "status": "available",
                "source": "licensed-test-fixture",
                "as_of": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "checksum": "sha256:fixture",
                "funds": {"SYNTH": {"technology": "1"}},
            }
        )


@pytest.mark.parametrize(
    "funds",
    [
        {"spy": {"technology": "1"}, "SPY": {"healthcare": "1"}},
        {"SPY": {"Technology": "0.5", "technology": "0.5"}},
    ],
)
def test_etf_sector_map_rejects_normalized_key_aliases(funds: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="duplicate normalized"):
        EtfSectorMap.from_mapping(
            {
                "schema_version": 1,
                "status": "available",
                "source": "licensed-test-fixture",
                "as_of": "2026-09-14",
                "checksum": "sha256:fixture",
                "funds": funds,
            }
        )
