from __future__ import annotations

from data.quarantine import QuarantineStore


def test_quarantine_store_marks_and_releases_symbol(tmp_path) -> None:
    store = QuarantineStore(tmp_path / "quarantine.jsonl")
    store.quarantine(symbol="AAPL", data_type="quote", reason="outlier", payload={"c": 1})
    assert store.is_quarantined(symbol="AAPL", data_type="quote") is True

    store.release(symbol="AAPL", data_type="quote")
    assert store.is_quarantined(symbol="AAPL", data_type="quote") is False


def test_quarantine_store_persists_records(tmp_path) -> None:
    path = tmp_path / "quarantine.jsonl"
    store = QuarantineStore(path)
    store.quarantine(symbol="MSFT", data_type="fundamentals", reason="empty", payload={})
    records = store.list_records(include_released=True)
    assert records
    assert records[0]["symbol"] == "MSFT"
