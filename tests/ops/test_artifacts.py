"""Artifact factories must load the same qualified inputs that their hashes identify."""

import json

import pytest

from ops.artifacts import InstalledArtifacts
from tests.backtest.test_datasets import T, manifest, price_record, risk_contract


def test_unqualified_data_file_cannot_become_runtime_provider(tmp_path):
    path = tmp_path / "data.json"
    path.write_text("{}")
    with pytest.raises(ValueError):
        InstalledArtifacts.load_data(path)


def test_loaded_market_keeps_original_raw_price_and_availability(tmp_path):
    row = price_record("price", T.date(), T)
    row["reference_close"] = "50"
    path = tmp_path / "bundle.json"
    path.write_text(
        json.dumps({"manifest": manifest([row], risk_contract=risk_contract()), "records": [row]})
    )
    loaded = InstalledArtifacts.load_data(path)
    assert loaded.bundle.records[0]["close"] == "100"
    assert loaded.bundle.records[0]["reference_close"] == "50"
    assert loaded.bundle.records[0]["available_at"] == T.isoformat()
