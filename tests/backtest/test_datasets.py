from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from backtest.broker import CausalBacktestBrokerAdapter
from backtest.datasets import (
    DatasetManifest,
    action_application_time,
    load_dataset_bundle,
    qualified_risk_service_factory,
    records_checksum,
    validate_adjustment_compatibility,
    visible_records,
    visible_universe,
)
from backtest.engine import (
    BacktestEngine,
    BacktestRunConfig,
    QualifiedDatasetLoader,
    _apply_corporate_action,
    _apply_visible_action,
    _BacktestIngestionStub,
    _canonical_bar_snapshot,
)
from ops.calendar import USTradingCalendar
from portfolio.accounting import AccountingState
from portfolio.broker import BrokerOrder
from portfolio.journal import EconomicEvent, TradePayload
from portfolio.local_economic import LocalEconomicEventStore

T = datetime(2026, 9, 14, 20, tzinfo=timezone.utc)


def record(identifier, kind, *, available=T, revision="v1", **values):
    return {
        "record_id": identifier,
        "kind": kind,
        "symbol": "SPY",
        "event_at": T.isoformat(),
        "available_at": available.isoformat(),
        "source": "synthetic:qualified-dataset",
        "revision": revision,
        "checksum": hashlib.sha256(identifier.encode()).hexdigest(),
        **values,
    }


def price_record(identifier, session, close_at, *, available=None, revision="v1", close="100"):
    return record(
        identifier,
        "price",
        available=available or close_at,
        revision=revision,
        event_at=close_at.isoformat(),
        session=session.isoformat(),
        open=close,
        high=str(Decimal(close) + 1),
        low=str(Decimal(close) - 1),
        close=close,
        reference_close=close,
        volume="1000000",
    )


def manifest(
    records, *, price_convention="raw", universe_policy="point_in_time", risk_contract=None
):
    result = {
        "schema_version": 1,
        "dataset_id": "synthetic-qualified-v1",
        "created_at": T.isoformat(),
        "source": "synthetic:qualified-dataset",
        "license": "test-fixture-only",
        "records_checksum": records_checksum(tuple(records)),
        "price_convention": price_convention,
        "calendar": "XNYS",
        "universe_policy": universe_policy,
        "coverage_start": "2026-09-14",
        "coverage_end": "2026-09-18",
        "limitations": ["synthetic fixture; no performance evidence"],
    }
    if risk_contract is not None:
        result["risk_contract"] = risk_contract
    return result


def risk_contract():
    return {
        "policy": {"max_slippage_fraction": "0.02"},
        "freshness_seconds": {"mark": 86400, "classification": 10000000, "liquidity": 10000000},
        "artifact_ttl_seconds": 120,
        "reference_price_convention": "split_adjusted",
    }


def test_publication_lag_and_revision_visibility_are_point_in_time():
    first = record("fundamental", "fundamental", value={"revenue": "12"})
    revised = record(
        "fundamental",
        "fundamental",
        available=T + timedelta(days=2),
        revision="v2",
        value={"revenue": "10"},
    )
    assert visible_records((first, revised), T + timedelta(days=1)) == (first,)
    assert visible_records((first, revised), T + timedelta(days=3)) == (revised,)


def test_qualified_risk_contract_requires_split_adjusted_reference_prices(tmp_path):
    row = price_record("price", T.date(), T)
    row.pop("reference_close")
    path = tmp_path / "missing-reference.json"
    path.write_text(
        json.dumps({"manifest": manifest([row], risk_contract=risk_contract()), "records": [row]})
    )
    with pytest.raises(ValueError, match="require reference_close"):
        load_dataset_bundle(path)


def test_split_adjusted_reference_drives_decisions_while_raw_close_is_preserved(tmp_path):
    calendar = USTradingCalendar()
    prior_session = datetime(2026, 9, 11, tzinfo=timezone.utc).date()
    current_session = T.date()
    prior_close = calendar.session_bounds(prior_session)[1]
    current_close = calendar.session_bounds(current_session)[1]
    prior = price_record("prior", prior_session, prior_close, close="99")
    prior["reference_close"] = "49.5"
    current = price_record("current", current_session, current_close, close="100")
    current["reference_close"] = "50"
    rows = [prior, current]
    path = tmp_path / "split-reference.json"
    path.write_text(
        json.dumps({"manifest": manifest(rows, risk_contract=risk_contract()), "records": rows})
    )

    dataset = QualifiedDatasetLoader(load_dataset_bundle(path)).load(
        ["SPY"], prior_session, current_session
    )
    current_bar = dataset.get_bar("SPY", current_session, as_of=current_close)
    assert current_bar is not None
    assert current_bar.close == 100.0
    assert current_bar.reference_close == 50.0
    assert (
        dataset.previous_close("SPY", current_session, as_of=current_close, calendar=calendar)
        == 49.5
    )
    raw_previous = dataset.raw_previous_close(
        "SPY", current_session, as_of=current_close, calendar=calendar
    )
    assert raw_previous == 99.0
    snapshot = _canonical_bar_snapshot("SPY", current_bar, raw_previous, current_close)
    assert snapshot.quote.last == Decimal("100.0")
    assert snapshot.quote.previous_close == Decimal("99.0")
    ingestion = _BacktestIngestionStub()
    ingestion.set_snapshot(
        snapshot, reference_price=current_bar.reference_close, reference_previous_close=49.5
    )
    assert ingestion.get_reference_prices("SPY") == (Decimal("50.0"), Decimal("49.5"))


def test_risk_nav_values_raw_share_units_at_raw_close(tmp_path):
    price = price_record("price", T.date(), T, close="100")
    price["reference_close"] = "50"
    rows = [
        price,
        record("class", "risk_classification", asset_type="etf", sector=None),
        record("liquidity", "risk_liquidity", average_daily_volume="1000000"),
        record(
            "sectors",
            "etf_sector_map",
            as_of=T.date().isoformat(),
            weights={"technology": ".5", "financials": ".5"},
        ),
    ]
    path = tmp_path / "raw-risk-mark.json"
    path.write_text(
        json.dumps({"manifest": manifest(rows, risk_contract=risk_contract()), "records": rows})
    )
    bundle = load_dataset_bundle(path)
    store = SimpleNamespace(
        projection=lambda: {
            "cash": "1000",
            "realized_pnl": "0",
            "positions": {"SPY": {"quantity": "10", "average_cost": "100"}},
        }
    )
    broker = SimpleNamespace(working_reservations=lambda: ())
    service = qualified_risk_service_factory(bundle)(store, broker, SimpleNamespace(now=lambda: T))

    artifact = service.freeze(
        proposal_id="proposal", symbol="SPY", side="buy", quantity=1, worst_price=100
    )
    assert artifact.decision.nav == Decimal("2000")


def test_each_etf_mapping_date_is_validated_before_aggregation(tmp_path):
    rows = [
        price_record("price", T.date(), T),
        record("class", "risk_classification", asset_type="etf", sector=None),
        record("liquidity", "risk_liquidity", average_daily_volume="1000000"),
        record(
            "future-spy",
            "etf_sector_map",
            as_of=(T + timedelta(days=1)).date().isoformat(),
            weights={"technology": ".5", "financials": ".5"},
        ),
        record(
            "current-qqq",
            "etf_sector_map",
            symbol="QQQ",
            as_of=T.date().isoformat(),
            weights={"technology": ".5", "financials": ".5"},
        ),
    ]
    path = tmp_path / "mixed-etf-dates.json"
    path.write_text(
        json.dumps({"manifest": manifest(rows, risk_contract=risk_contract()), "records": rows})
    )
    bundle = load_dataset_bundle(path)
    store = SimpleNamespace(
        projection=lambda: {"cash": "1000", "realized_pnl": "0", "positions": {}}
    )
    broker = SimpleNamespace(working_reservations=lambda: ())
    service = qualified_risk_service_factory(bundle)(store, broker, SimpleNamespace(now=lambda: T))

    with pytest.raises(ValueError, match="not yet available"):
        service.freeze(
            proposal_id="proposal", symbol="SPY", side="buy", quantity=1, worst_price=100
        )


def test_same_identity_revision_at_same_availability_conflict_fails_closed():
    left = record("filing", "fundamental", value={"revenue": "12"})
    right = record("filing", "fundamental", revision="v2", value={"revenue": "10"})
    with pytest.raises(ValueError, match="ambiguous revision"):
        visible_records((left, right), T)


def test_historical_universe_includes_delisted_only_while_membership_visible():
    enter = record(
        "old-enter",
        "universe",
        symbol="OLD",
        effective_at="2020-01-02T21:00:00+00:00",
        member=True,
    )
    leave = record(
        "old-leave",
        "universe",
        symbol="OLD",
        available=T + timedelta(days=2),
        effective_at=(T + timedelta(days=2)).isoformat(),
        member=False,
    )
    assert visible_universe((enter, leave), T + timedelta(days=1)) == ("OLD",)
    assert visible_universe((enter, leave), T + timedelta(days=3)) == ()


def test_split_and_dividend_apply_only_when_effective_payable_and_visible():
    split = record(
        "split",
        "corporate_action",
        action_type="split",
        ratio="2",
        effective_at=(T + timedelta(days=1)).isoformat(),
    )
    dividend = record(
        "dividend",
        "corporate_action",
        action_type="cash_dividend",
        amount="0.25",
        currency="USD",
        entitlement_at=(T + timedelta(days=2)).isoformat(),
        payable_at=(T + timedelta(days=3)).isoformat(),
    )
    assert action_application_time(split) == T + timedelta(days=1)
    assert action_application_time(dividend) == T + timedelta(days=3)


@pytest.mark.parametrize(
    "convention,action_type",
    [
        ("split_adjusted", "split"),
        ("total_return_adjusted", "split"),
        ("total_return_adjusted", "cash_dividend"),
    ],
)
def test_adjusted_price_convention_rejects_double_applied_actions(convention, action_type):
    action = record(
        "action",
        "corporate_action",
        action_type=action_type,
        ratio="2" if action_type == "split" else None,
        amount="0.25" if action_type == "cash_dividend" else None,
        payable_at=T.isoformat() if action_type == "cash_dividend" else None,
        entitlement_at=T.isoformat() if action_type == "cash_dividend" else None,
        effective_at=T.isoformat() if action_type == "split" else None,
    )
    with pytest.raises(ValueError, match="double"):
        validate_adjustment_compatibility(convention, (action,))


def test_static_universe_requires_explicit_survivorship_limitation():
    with pytest.raises(ValueError, match="survivorship"):
        DatasetManifest.from_mapping(
            manifest([], universe_policy="static") | {"limitations": ["synthetic"]}
        )


def test_bundle_checksum_tamper_and_missing_license_fail_without_import(tmp_path):
    rows = [
        record(
            "price",
            "price",
            session=T.date().isoformat(),
            open="100",
            high="101",
            low="99",
            close="100",
            volume="10",
        )
    ]
    payload = {"manifest": manifest(rows), "records": rows}
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(payload))
    assert load_dataset_bundle(path).manifest.dataset_id == "synthetic-qualified-v1"
    payload["records"][0]["close"] = "999"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="checksum"):
        load_dataset_bundle(path)
    payload["manifest"]["license"] = ""
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="license"):
        load_dataset_bundle(path)


def test_invalid_nonfinite_price_and_naive_availability_fail_closed():
    bad_price = record(
        "price",
        "price",
        session=T.date().isoformat(),
        open="100",
        high="101",
        low="99",
        close="NaN",
        volume="10",
    )
    with pytest.raises(ValueError, match="finite"):
        records_checksum((bad_price,))
    naive = record("news", "news", value={"headline": "known later"})
    naive["available_at"] = "2026-09-14T20:00:00"
    with pytest.raises(ValueError, match="timezone"):
        visible_records((naive,), T)


def test_price_revision_is_selected_only_after_its_availability(tmp_path):
    calendar = USTradingCalendar()
    session = datetime(2024, 1, 2, tzinfo=timezone.utc).date()
    close_at = calendar.session_bounds(session)[1]
    first = price_record("price-spy", session, close_at, close="100")
    revised = price_record(
        "price-spy",
        session,
        close_at,
        available=close_at + timedelta(days=2),
        revision="v2",
        close="101",
    )
    path = tmp_path / "revisions.json"
    path.write_text(
        json.dumps({"manifest": manifest([first, revised]), "records": [first, revised]})
    )
    bundle = load_dataset_bundle(path)
    dataset = QualifiedDatasetLoader(bundle).load(["SPY"], session, session)
    assert dataset.get_bar("SPY", session, as_of=close_at).close == 100
    assert dataset.get_bar("SPY", session, as_of=close_at + timedelta(days=3)).close == 101


def test_conflicting_price_revisions_at_same_availability_fail_closed(tmp_path):
    calendar = USTradingCalendar()
    session = datetime(2024, 1, 2, tzinfo=timezone.utc).date()
    close_at = calendar.session_bounds(session)[1]
    rows = [
        price_record("price-spy", session, close_at, close="100"),
        price_record("price-spy", session, close_at, revision="v2", close="101"),
    ]
    path = tmp_path / "conflict.json"
    path.write_text(json.dumps({"manifest": manifest(rows), "records": rows}))
    dataset = QualifiedDatasetLoader(load_dataset_bundle(path)).load(["SPY"], session, session)
    with pytest.raises(ValueError, match="ambiguous price revision"):
        dataset.get_bar("SPY", session, as_of=close_at)


def test_dividend_entitlement_is_stable_across_sell_and_replay(tmp_path):
    store = LocalEconomicEventStore(
        tmp_path / "events.json",
        genesis=AccountingState(Decimal("1000"), Decimal("0"), {}),
        account_id="backtest",
        mode="simulated",
    )
    store.apply_event(
        EconomicEvent(
            "backtest",
            "simulated",
            "buy",
            T,
            "buy-source",
            TradePayload("buy-order", "SPY", Decimal("10"), Decimal("10"), Decimal("0")),
        )
    )
    dividend = record(
        "dividend",
        "corporate_action",
        available=T,
        action_type="cash_dividend",
        amount="1",
        currency="USD",
        entitlement_at=T.isoformat(),
        payable_at=(T + timedelta(days=2)).isoformat(),
    )
    _apply_corporate_action(store, dividend)
    store = LocalEconomicEventStore(
        tmp_path / "events.json",
        genesis=AccountingState(Decimal("1000"), Decimal("0"), {}),
        account_id="backtest",
        mode="simulated",
    )
    store.apply_event(
        EconomicEvent(
            "backtest",
            "simulated",
            "sell",
            T + timedelta(days=3),
            "sell-source",
            TradePayload("sell-order", "SPY", Decimal("-1"), Decimal("10"), Decimal("0")),
        )
    )
    _apply_corporate_action(store, dividend)
    assert [event.event_id for event in store.events()] == ["buy", "dataset:dividend", "sell"]
    assert store.projection()["cash"] == "920"


def test_new_pending_split_blocks_but_replayed_split_ignores_later_order(tmp_path):
    store = LocalEconomicEventStore(
        tmp_path / "split.json",
        genesis=AccountingState(Decimal("1000"), Decimal("0"), {}),
        account_id="backtest",
        mode="simulated",
    )
    broker = CausalBacktestBrokerAdapter(store, now=lambda: T)
    split = record(
        "split",
        "corporate_action",
        action_type="split",
        ratio="2",
        effective_at=T.isoformat(),
    )
    broker.submit_order(BrokerOrder("before", "SPY", 2, "buy", 100))
    with pytest.raises(RuntimeError, match="pending-order adjustment"):
        _apply_visible_action(store, broker, split)
    assert store.events() == ()

    broker.cancel_order("bt-before")
    _apply_visible_action(store, broker, split)
    broker.submit_order(BrokerOrder("after", "SPY", 2, "buy", 100))
    _apply_visible_action(store, broker, split)
    assert [event.event_id for event in store.events()] == ["dataset:split"]


def test_engine_uses_one_event_timeline_for_fill_split_and_dividend(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_COUNCIL_MIN_SUPPORT", "1")
    calendar = USTradingCalendar()
    sessions = []
    cursor = datetime(2024, 1, 2, tzinfo=timezone.utc).date()
    while len(sessions) < 65:
        bounds = calendar.session_bounds(cursor)
        if bounds is not None:
            sessions.append((cursor, bounds[1]))
        cursor += timedelta(days=1)
    rows = []
    for index, (session, close_at) in enumerate(sessions):
        close = "101" if index == 61 else ("110" if index >= 63 else "100")
        rows.append(price_record(f"price-{session.isoformat()}", session, close_at, close=close))
    rows.append(
        record(
            "universe-spy",
            "universe",
            available=sessions[0][1],
            effective_at=sessions[0][1].isoformat(),
            member=True,
        )
    )
    risk_at = sessions[61][1]
    rows.extend(
        (
            record(
                "risk-class",
                "risk_classification",
                available=risk_at,
                event_at=risk_at.isoformat(),
                asset_type="etf",
                sector=None,
            ),
            record(
                "risk-liquidity",
                "risk_liquidity",
                available=risk_at,
                event_at=risk_at.isoformat(),
                average_daily_volume="1000000",
            ),
            record(
                "risk-sectors",
                "etf_sector_map",
                available=risk_at,
                event_at=risk_at.isoformat(),
                as_of=risk_at.date().isoformat(),
                weights={"technology": "0.5", "financials": "0.5"},
            ),
        )
    )
    rows.append(
        record(
            "universe-spy-exit",
            "universe",
            available=sessions[62][1],
            effective_at=sessions[62][1].isoformat(),
            member=False,
        )
    )
    rows.append(
        record(
            "split-spy",
            "corporate_action",
            available=sessions[0][1],
            action_type="split",
            ratio="0.5",
            effective_at=sessions[63][1].isoformat(),
        )
    )
    rows.append(
        record(
            "dividend-spy",
            "corporate_action",
            available=sessions[0][1],
            action_type="cash_dividend",
            amount="0.25",
            currency="USD",
            entitlement_at=sessions[63][1].isoformat(),
            payable_at=sessions[64][1].isoformat(),
        )
    )
    path = tmp_path / "bundle.json"
    path.write_text(
        json.dumps({"manifest": manifest(rows, risk_contract=risk_contract()), "records": rows})
    )
    bundle = load_dataset_bundle(path)
    result = BacktestEngine(
        data_loader=QualifiedDatasetLoader(bundle),
        storage_dir=tmp_path / "runs",
        risk_service_factory=qualified_risk_service_factory(bundle),
    ).run(BacktestRunConfig(["SPY"], sessions[0][0], sessions[-1][0], 100_000))
    kinds = [item["payload"]["kind"] for item in result.economic_events]
    assert kinds == ["trade", "split", "cash"]
    assert Decimal(result.economic_events[-1]["payload"]["amount"]) == Decimal("4.875")
    assert result.dataset_manifest["dataset_id"] == "synthetic-qualified-v1"
    assert Decimal(str(result.final_nav)) == Decimal("98247.9")

    future = record(
        "risk-liquidity",
        "risk_liquidity",
        available=sessions[-1][1] + timedelta(days=1),
        revision="v2",
        event_at=risk_at.isoformat(),
        average_daily_volume="1",
    )
    extended_path = tmp_path / "extended.json"
    extended_rows = [*rows, future]
    extended_path.write_text(
        json.dumps(
            {
                "manifest": manifest(extended_rows, risk_contract=risk_contract()),
                "records": extended_rows,
            }
        )
    )
    extended = BacktestEngine(
        data_loader=QualifiedDatasetLoader(load_dataset_bundle(extended_path)),
        storage_dir=tmp_path / "extended-runs",
        risk_service_factory=qualified_risk_service_factory(load_dataset_bundle(extended_path)),
    ).run(BacktestRunConfig(["SPY"], sessions[0][0], sessions[-1][0], 100_000))
    assert extended.nav_series == result.nav_series

    def economics(events):
        return [
            {
                key: value
                for key, value in item["payload"].items()
                if key not in {"order_id", "fee_reference"}
            }
            for item in events
        ]

    assert economics(extended.economic_events) == economics(result.economic_events)

    missing_rows = [
        item
        for item in rows
        if not (item["kind"] == "price" and item["session"] == sessions[-1][0].isoformat())
    ]
    missing_path = tmp_path / "missing-held-mark.json"
    missing_path.write_text(
        json.dumps(
            {
                "manifest": manifest(missing_rows, risk_contract=risk_contract()),
                "records": missing_rows,
            }
        )
    )
    with pytest.raises(RuntimeError, match="lacks a visible valuation mark"):
        missing_bundle = load_dataset_bundle(missing_path)
        BacktestEngine(
            data_loader=QualifiedDatasetLoader(missing_bundle),
            storage_dir=tmp_path / "missing-runs",
            risk_service_factory=qualified_risk_service_factory(missing_bundle),
        ).run(BacktestRunConfig(["SPY"], sessions[0][0], sessions[-1][0], 100_000))
