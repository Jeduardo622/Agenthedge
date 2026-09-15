from decimal import Decimal as D

import pytest

from learning.attribution import allocate_realized_pnl, attribute_economic_envelopes


def test_realized_pnl_belongs_to_normalized_entry_owners():
    assert allocate_realized_pnl({"momentum": D("3"), "value": D("1")}, D("20")) == {
        "momentum": D("15"),
        "value": D("5"),
    }


def test_decimal_allocation_conserves_every_fractional_remainder():
    result = allocate_realized_pnl({"a": D("1"), "b": D("1"), "c": D("1")}, D("0.01"))
    assert sum(result.values(), D(0)) == D("0.01")
    assert result == {
        "a": D("0.003333333333333333333333333333"),
        "b": D("0.003333333333333333333333333333"),
        "c": D("0.003333333333333333333333333334"),
    }


@pytest.mark.parametrize(
    "weights",
    [
        {},
        {"a": D(0)},
        {"a": D(-1), "b": D(2)},
        {"a": D("NaN")},
        {"": D(1)},
        {"A": D(1), " a ": D(1)},
    ],
)
def test_invalid_or_ambiguous_entry_weights_fail_closed(weights):
    with pytest.raises(ValueError):
        allocate_realized_pnl(weights, D(1))


@pytest.mark.parametrize("realized", [D("NaN"), D("Infinity")])
def test_nonfinite_realized_pnl_fails_closed(realized):
    with pytest.raises(ValueError):
        allocate_realized_pnl({"momentum": D(1)}, realized)


def envelope(event_id, quantity, price, *, fee="0", owners=None, at="2026-09-14T14:00:00+00:00"):
    payload = {
        "kind": "trade",
        "order_id": event_id,
        "symbol": "SPY",
        "quantity": str(quantity),
        "price": str(price),
        "fee": str(fee),
    }
    if D(fee):
        payload["fee_reference"] = "fee:" + event_id
    return {
        "economic_event": {
            "account_id": "test",
            "mode": "simulated",
            "event_id": event_id,
            "occurred_at": at,
            "source_hash": event_id,
            "payload": payload,
        },
        **({"strategies": owners} if owners is not None else {}),
    }


def test_partial_exit_and_other_strategy_exit_credit_entry_owners_after_costs():
    events = [
        envelope(
            "entry",
            10,
            100,
            fee="2",
            owners=[
                {"strategy": "momentum", "confidence": 3},
                {"strategy": "value", "confidence": 1},
            ],
        ),
        envelope(
            "exit",
            -4,
            110,
            fee="1",
            owners=[{"strategy": "macro", "confidence": 1}],
            at="2026-09-14T15:00:00+00:00",
        ),
    ]
    result = attribute_economic_envelopes(events)
    assert result.realized_pnl == {"momentum": D("28.65"), "value": D("9.55")}
    assert result.unavailable_event_ids == ()


def test_reversal_excess_becomes_new_entry_owned_by_reversing_strategy():
    events = [
        envelope("long", 2, 100, owners=[{"strategy": "value", "confidence": 1}]),
        envelope(
            "reverse",
            -4,
            110,
            owners=[{"strategy": "macro", "confidence": 1}],
            at="2026-09-14T15:00:00+00:00",
        ),
        envelope(
            "cover",
            2,
            100,
            owners=[{"strategy": "momentum", "confidence": 1}],
            at="2026-09-14T16:00:00+00:00",
        ),
    ]
    assert attribute_economic_envelopes(events).realized_pnl == {
        "value": D("20"),
        "macro": D("20"),
    }


def test_missing_entry_owners_remains_visible_and_replay_is_idempotent():
    entry = envelope("entry", 1, 100)
    exit_event = envelope(
        "exit",
        -1,
        110,
        owners=[{"strategy": "exit", "confidence": 1}],
        at="2026-09-14T15:00:00+00:00",
    )
    result = attribute_economic_envelopes([entry, entry, exit_event])
    assert result.realized_pnl == {}
    assert result.unavailable_event_ids == ("entry", "exit")


def test_correction_rebuilds_original_entry_with_same_owners():
    entry = envelope("entry", 2, 100, owners=[{"strategy": "value", "confidence": 1}])
    correction = {
        "economic_event": {
            "account_id": "test",
            "mode": "simulated",
            "event_id": "correction",
            "occurred_at": "2026-09-14T14:30:00+00:00",
            "source_hash": "correction",
            "payload": {
                "kind": "correction",
                "reverses_event_id": "entry",
                "replacement": {**entry["economic_event"]["payload"], "price": "90"},
            },
        }
    }
    exit_event = envelope(
        "exit",
        -2,
        110,
        owners=[{"strategy": "macro", "confidence": 1}],
        at="2026-09-14T15:00:00+00:00",
    )
    assert attribute_economic_envelopes([entry, correction, exit_event]).realized_pnl == {
        "value": D("40")
    }


def test_same_event_on_two_topics_merges_explicit_owner_evidence():
    entry = envelope("entry", 1, 100, owners=[{"strategy": "value", "confidence": 1}])
    duplicate_without_intent = {"economic_event": entry["economic_event"]}
    exit_event = envelope("exit", -1, 110, at="2026-09-14T15:00:00+00:00")
    result = attribute_economic_envelopes([duplicate_without_intent, entry, exit_event])
    assert result.realized_pnl == {"value": D("10")}


def test_split_and_explicit_fee_reference_are_replayed_once():
    entry = envelope("entry", 1, 100, owners=[{"strategy": "value", "confidence": 1}])
    split = {
        "economic_event": {
            "account_id": "test",
            "mode": "simulated",
            "event_id": "split",
            "occurred_at": "2026-09-14T14:30:00+00:00",
            "source_hash": "split",
            "payload": {"kind": "split", "symbol": "SPY", "ratio": "2"},
        }
    }
    exit_event = envelope("exit", -2, 60, fee="2", at="2026-09-14T15:00:00+00:00")
    duplicate_fee = {
        "economic_event": {
            "account_id": "test",
            "mode": "simulated",
            "event_id": "fee",
            "occurred_at": "2026-09-14T15:00:01+00:00",
            "source_hash": "fee",
            "payload": {
                "kind": "cash",
                "amount": "-2",
                "reason": "fee",
                "fee_reference": "fee:exit",
            },
        }
    }
    result = attribute_economic_envelopes([entry, split, exit_event, duplicate_fee])
    assert result.realized_pnl == {"value": D("18")}
    assert result.unavailable_event_ids == ()


def test_cash_fee_before_referenced_trade_is_attributed_to_entry_once():
    owners = [{"strategy": "value", "confidence": 1}]
    cash_fee = {
        "economic_event": {
            "account_id": "test",
            "mode": "simulated",
            "event_id": "cash-fee",
            "occurred_at": "2026-09-14T13:00:00+00:00",
            "source_hash": "cash-fee",
            "payload": {
                "kind": "cash",
                "reason": "fee",
                "amount": "-2",
                "fee_reference": "fee:entry",
                "symbol": "SPY",
            },
        }
    }
    entry = envelope("entry", 1, 100, fee="2", owners=owners)
    exit_event = envelope("exit", -1, 110, at="2026-09-14T15:00:00+00:00")
    result = attribute_economic_envelopes([cash_fee, entry, exit_event])
    assert result.realized_pnl == {"value": D("8")}
    assert result.unavailable_event_ids == ()


def test_duplicate_fee_reference_across_trades_charges_once():
    owners = [{"strategy": "value", "confidence": 1}]
    entry = envelope("entry", 1, 100, fee="2", owners=owners)
    second = envelope("second", 1, 100, fee="2", owners=owners, at="2026-09-14T14:01:00+00:00")
    second["economic_event"]["payload"]["fee_reference"] = "fee:entry"
    exit_event = envelope("exit", -2, 110, at="2026-09-14T15:00:00+00:00")
    assert attribute_economic_envelopes([entry, second, exit_event]).realized_pnl == {
        "value": D("18")
    }


def test_event_ids_are_bound_to_one_account_and_mode_namespace():
    first = envelope("same", 1, 100, owners=[{"strategy": "value", "confidence": 1}])
    second = envelope("other", -1, 110)
    second["economic_event"]["account_id"] = "different"
    with pytest.raises(ValueError, match="namespace"):
        attribute_economic_envelopes([first, second])
