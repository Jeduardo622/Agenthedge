"""Performance tracker for adaptive strategy weighting."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping

from learning.attribution import attribute_economic_envelopes
from learning.promotion import StrategyAcceptance


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_stats() -> Dict[str, Any]:
    return {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "realized_pnl": 0.0,
        "attributed_realized_pnl": "0",
        "avg_confidence": 0.0,
        "penalties": 0,
        "weight": 1.0,
        "candidate_weight": 1.0,
        "last_updated": _now(),
    }


class PerformanceTracker:
    """Persists per-strategy metrics and derives adaptive weights."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._adopt(self._load())

    def _adopt(self, state: Mapping[str, Any]) -> None:
        self._strategies: Dict[str, Dict[str, Any]] = state.get("strategies", {})
        self._last_realized_pnl: float | None = state.get("last_realized_pnl")
        self._receipts: Dict[str, str] = state.get("receipts", {})
        self._attribution_events: Dict[str, Dict[str, Any]] = state.get("attribution_events", {})
        self._attribution_unavailable: list[str] = state.get("attribution_unavailable", [])
        self._namespace: dict[str, str] | None = state.get("namespace")
        self._installation: dict[str, Any] | None = state.get("installation")

    def record_fill(self, payload: Mapping[str, Any], *, receipt_key: str | None = None) -> None:
        strategies = payload.get("strategies") or []
        economic = payload.get("economic_event")
        if not isinstance(strategies, list):
            raise ValueError("invalid strategy entries")
        if not strategies and not isinstance(economic, Mapping):
            return
        key = _fill_identity(payload, receipt_key)
        # Current portfolio projections can differ on replay. Bind source economics,
        # not the later projection or transport receipt timestamp.
        fingerprint = _fingerprint(
            {
                name: payload.get(name)
                for name in ("symbol", "quantity", "price", "strategies", "economic_event")
            }
        )
        with self._lock:
            self._require_economic_namespace(payload)
            if self._already_applied(key, fingerprint):
                return
            state = self.to_dict()
            for strategy_entry in strategies:
                if not isinstance(strategy_entry, Mapping):
                    raise ValueError("invalid strategy entry")
                name = strategy_entry.get("strategy")
                if not isinstance(name, str) or not name:
                    raise ValueError("invalid strategy identity")
                confidence = strategy_entry.get("confidence")
                confidence_value = _finite(confidence) if confidence is not None else 0.0
                stats = state["strategies"].setdefault(name, _default_stats())
                stats["trades"] += 1
                stats["avg_confidence"] = _rolling_average(
                    stats["avg_confidence"], stats["trades"], confidence_value
                )
                stats["last_updated"] = _now()
                stats["candidate_weight"] = _recompute_weight(stats)
            if isinstance(economic, Mapping):
                event_id = economic.get("event_id")
                if not isinstance(event_id, str) or not event_id:
                    raise ValueError("economic attribution identity unavailable")
                attribution = {"economic_event": dict(economic), "strategies": strategies}
                _merge_attribution_event(state, event_id, attribution)
                _apply_attribution(state)
            state["receipts"][key] = fingerprint
            self._commit(state)

    def record_economic_event(
        self, payload: Mapping[str, Any], *, receipt_key: str | None = None
    ) -> None:
        economic = payload.get("economic_event")
        if not isinstance(economic, Mapping):
            raise ValueError("canonical economic event required")
        event_id = economic.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("economic attribution identity unavailable")
        fingerprint = _fingerprint(
            {"economic_event": economic, "strategies": payload.get("strategies")}
        )
        key = "economic:" + event_id
        with self._lock:
            self._require_economic_namespace(payload)
            if self._already_applied(key, fingerprint):
                return
            state = self.to_dict()
            attribution = {"economic_event": dict(economic)}
            if payload.get("strategies") is not None:
                attribution["strategies"] = payload["strategies"]
            _merge_attribution_event(state, event_id, attribution)
            _apply_attribution(state)
            state["receipts"][key] = fingerprint
            self._commit(state)

    def _require_economic_namespace(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            if self._namespace is None:
                return
            economic = payload.get("economic_event")
            if not isinstance(economic, Mapping) or any(
                economic.get(key) != value for key, value in self._namespace.items()
            ):
                raise ValueError("installed learning economic namespace mismatch")

    def apply_feedback(
        self,
        strategy: str,
        delta: float,
        reason: str | None = None,
        *,
        receipt_key: str | None = None,
    ) -> None:
        if not strategy:
            return
        delta = _finite(delta)
        fingerprint = _fingerprint({"strategy": strategy, "delta": delta, "reason": reason})
        key = "feedback:" + receipt_key if receipt_key else None
        with self._lock:
            if key and self._already_applied(key, fingerprint):
                return
            state = self.to_dict()
            stats = state["strategies"].setdefault(strategy, _default_stats())
            if delta < 0:
                stats["penalties"] += 1
            candidate = round(max(0.1, min(2.5, stats["weight"] + delta)), 4)
            stats["candidate_weight"] = candidate
            if delta < 0:
                stats["weight"] = min(stats["weight"], candidate)
            stats["last_feedback"] = {
                "reason": reason,
                "delta": delta,
                "timestamp": _now(),
            }
            stats["last_updated"] = _now()
            if key:
                state["receipts"][key] = fingerprint
            self._commit(state)

    def install_accepted_weights(self, acceptance: StrategyAcceptance) -> None:
        """Atomically install an entire signed roster without manufacturing feedback."""
        if type(acceptance) is not StrategyAcceptance:
            raise TypeError("controller-owned signed strategy acceptance required")
        document = json.loads(acceptance.manifest)
        approved = document.get("strategy_weights") if isinstance(document, dict) else None
        if (
            not isinstance(approved, dict)
            or not approved
            or any(
                not isinstance(name, str) or not name or name.strip() != name for name in approved
            )
        ):
            raise ValueError("explicit complete approved strategy weights required")
        identity = acceptance.trust.expected
        namespace = {"account_id": identity.account_id, "mode": identity.mode}
        with self._lock:
            state = self.to_dict()
            if self._namespace is not None and self._namespace != namespace:
                raise ValueError("accepted strategy namespace mismatch")
            if self._namespace is None and any(
                state[key]
                for key in (
                    "strategies",
                    "receipts",
                    "attribution_events",
                    "attribution_unavailable",
                )
            ):
                raise ValueError("nonempty legacy learning state requires explicit migration")
            if self._namespace is None and state["last_realized_pnl"] is not None:
                raise ValueError("nonempty legacy learning state requires explicit migration")
            installed = self._installation
            retired = list(installed["retired_hashes"]) if installed else []
            if identity.strategy_hash in retired:
                raise ValueError("retired strategy identity cannot be reinstalled")
            if installed and not set(installed["approved_weights"]) <= set(approved):
                raise ValueError("new manifest cannot omit an installed strategy")
            same = installed is not None and installed["strategy_hash"] == identity.strategy_hash
            caps: dict[str, float] = {}
            for name, raw_weight in approved.items():
                stats = state["strategies"].setdefault(name, _default_stats())
                revision = stats["accepted_safety_revision"] if same else stats["penalties"]
                acceptance.require_candidate(name, raw_weight, safety_revision=revision)
                weight = float(Decimal(str(raw_weight)))
                caps[name] = weight
                if same:
                    # A restart is not a promotion. Retain candidate and safety history.
                    stats["weight"] = min(stats["weight"], weight)
                else:
                    stats.update(
                        weight=weight,
                        candidate_weight=weight,
                        active_strategy_hash=identity.strategy_hash,
                        accepted_strategy_hash=identity.strategy_hash,
                        accepted_safety_revision=revision,
                        accepted_candidate_version=_candidate_version(
                            name, identity.strategy_hash, weight, safety_revision=revision
                        ),
                    )
            if installed and not same:
                retired.append(installed["strategy_hash"])
            state["namespace"] = namespace
            state["installation"] = {
                "strategy_hash": identity.strategy_hash,
                "approved_weights": caps,
                "retired_hashes": retired,
            }
            self._commit(state)

    def installed_weights(self) -> Dict[str, float] | None:
        """Active installed roster, bounded by its signed caps; None means legacy unbound."""
        with self._lock:
            if self._installation is None:
                return None
            return {
                name: min(self._strategies[name]["weight"], cap)
                for name, cap in self._installation["approved_weights"].items()
            }

    def activate_candidate_weight(self, strategy: str, *, acceptance: StrategyAcceptance) -> None:
        if not strategy or type(acceptance) is not StrategyAcceptance:
            raise TypeError("controller-owned signed strategy acceptance required")
        with self._lock:
            identity = acceptance.trust.expected
            if self._namespace is not None and self._namespace != {
                "account_id": identity.account_id,
                "mode": identity.mode,
            }:
                raise ValueError("accepted strategy namespace mismatch")
            if (
                self._installation is not None
                and identity.strategy_hash != self._installation["strategy_hash"]
            ):
                raise ValueError("install the complete new signed strategy roster atomically")
            if (
                self._installation is not None
                and strategy not in self._installation["approved_weights"]
            ):
                raise ValueError("strategy absent from installed roster")
            state = self.to_dict()
            stats = state["strategies"].get(strategy)
            if stats is None:
                raise ValueError("strategy candidate unavailable")
            safety_revision = stats["penalties"]
            strategy_hash = acceptance.require_candidate(
                strategy, stats["candidate_weight"], safety_revision=safety_revision
            )
            accepted_strategy_hash = strategy_hash
            stats["weight"] = stats["candidate_weight"]
            candidate_version = _candidate_version(
                strategy, strategy_hash, stats["candidate_weight"], safety_revision=safety_revision
            )
            prior_acceptance = stats.get("accepted_strategy_hash")
            prior_version = stats.get("accepted_candidate_version")
            if prior_acceptance == accepted_strategy_hash and prior_version != candidate_version:
                raise ValueError("new acceptance required for changed candidate version")
            stats["active_strategy_hash"] = strategy_hash
            stats["accepted_strategy_hash"] = accepted_strategy_hash
            stats["accepted_candidate_version"] = candidate_version
            stats["accepted_safety_revision"] = safety_revision
            stats["last_updated"] = _now()
            self._commit(state)

    def _already_applied(self, key: str, fingerprint: str) -> bool:
        previous = self._receipts.get(key)
        if previous is not None and previous != fingerprint:
            raise ValueError("performance receipt identity conflict; recovery required")
        return previous is not None

    def snapshot(self) -> Mapping[str, Mapping[str, Any]]:
        with self._lock:
            return deepcopy(self._strategies)

    def weights(self) -> Dict[str, float]:
        with self._lock:
            return {name: stats.get("weight", 1.0) for name, stats in self._strategies.items()}

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return deepcopy(
                {
                    "strategies": self._strategies,
                    "last_realized_pnl": self._last_realized_pnl,
                    "receipts": self._receipts,
                    "attribution_events": self._attribution_events,
                    "attribution_unavailable": self._attribution_unavailable,
                    "namespace": self._namespace,
                    "installation": self._installation,
                }
            )

    def _load(self) -> MutableMapping[str, Any]:
        if not self._path.exists():
            return {
                "strategies": {},
                "last_realized_pnl": None,
                "receipts": {},
                "attribution_events": {},
                "attribution_unavailable": [],
            }
        try:
            data = json.loads(self._path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError("corrupt performance state; recovery required") from exc
        if not isinstance(data, MutableMapping):
            raise ValueError("invalid performance state; recovery required")
        strategies = data.get("strategies")
        if not isinstance(strategies, MutableMapping):
            raise ValueError("invalid performance strategies; recovery required")
        for name, stats in strategies.items():
            if not isinstance(name, str) or not name or not isinstance(stats, dict):
                raise ValueError("invalid performance strategy state")
            for field in ("trades", "wins", "losses", "penalties"):
                if type(stats.get(field)) is not int or stats[field] < 0:
                    raise ValueError("invalid performance counters")
            for field in ("realized_pnl", "avg_confidence", "weight"):
                _finite(stats.get(field))
            if "candidate_weight" not in stats:
                stats["candidate_weight"] = stats["weight"]
            _finite(stats["candidate_weight"])
        if data.get("last_realized_pnl") is not None:
            _finite(data["last_realized_pnl"])
        receipts = data.get("receipts", {})
        if not isinstance(receipts, dict) or any(
            not isinstance(key, str) or not key or not isinstance(value, str) or len(value) != 64
            for key, value in receipts.items()
        ):
            raise ValueError("invalid performance receipts")
        attribution_events = data.get("attribution_events", {})
        unavailable = data.get("attribution_unavailable", [])
        if not isinstance(attribution_events, dict) or not isinstance(unavailable, list):
            raise ValueError("invalid attribution state")
        rebuilt = attribute_economic_envelopes(tuple(attribution_events.values()))
        if list(rebuilt.unavailable_event_ids) != unavailable:
            raise ValueError("attribution state does not reproduce")
        if attribution_events:
            for name, stats in strategies.items():
                expected = rebuilt.realized_pnl.get(str(name).strip().casefold(), Decimal(0))
                exact = stats.get("attributed_realized_pnl")
                if (
                    exact is None
                    or Decimal(exact) != expected
                    or stats["realized_pnl"] != float(expected)
                ):
                    raise ValueError("attribution projection does not reproduce")
        namespace, installation = data.get("namespace"), data.get("installation")
        if (namespace is None) != (installation is None):
            raise ValueError("incomplete installed learning namespace")
        if namespace is not None:
            if (
                not isinstance(namespace, dict)
                or set(namespace) != {"account_id", "mode"}
                or not isinstance(namespace["account_id"], str)
                or not namespace["account_id"]
                or namespace["account_id"].strip() != namespace["account_id"]
                or namespace["mode"] not in {"simulated", "paper_broker", "live"}
                or not isinstance(installation, dict)
                or set(installation) != {"strategy_hash", "approved_weights", "retired_hashes"}
            ):
                raise ValueError("invalid installed learning namespace")
            hashes = (
                [installation["strategy_hash"], *installation["retired_hashes"]]
                if isinstance(installation["retired_hashes"], list)
                else []
            )
            if (
                not hashes
                or any(
                    not isinstance(item, str)
                    or len(item) != 64
                    or any(c not in "0123456789abcdef" for c in item)
                    for item in hashes
                )
                or len(hashes) != len(set(hashes))
            ):
                raise ValueError("invalid installed strategy identity")
            caps = installation["approved_weights"]
            if not isinstance(caps, dict) or not caps or not set(caps) <= set(strategies):
                raise ValueError("invalid installed strategy roster")
            for name, cap in caps.items():
                stats = strategies[name]
                revision = stats.get("accepted_safety_revision")
                if (
                    not 0 < _finite(cap) <= 2.5
                    or not 0 < stats["weight"] <= cap
                    or type(revision) is not int
                    or not 0 <= revision <= stats["penalties"]
                    or stats.get("accepted_strategy_hash") != installation["strategy_hash"]
                ):
                    raise ValueError("invalid installed strategy cap or safety revision")
        return {
            "namespace": namespace,
            "installation": installation,
            "strategies": {str(name): dict(stats) for name, stats in strategies.items()},
            "last_realized_pnl": data.get("last_realized_pnl"),
            "receipts": dict(receipts),
            "attribution_events": dict(attribution_events),
            "attribution_unavailable": list(unavailable),
        }

    def _commit(self, state: Dict[str, Any]) -> None:
        """One process owns this file; effect and receipt share one atomic replace."""
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=self._path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary = output.name
                json.dump(state, output, allow_nan=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
        except Exception:
            self._adopt(self._load())
            raise
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        self._adopt(state)


def _finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("performance number must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("performance number must be finite")
    return result


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _fill_identity(payload: Mapping[str, Any], fallback: str | None) -> str:
    fields = tuple(payload.get(name) for name in ("account_id", "mode", "event_id"))
    if any(value is not None for value in fields):
        if not all(isinstance(value, str) and value.strip() for value in fields):
            raise ValueError("incomplete economic event identity")
        return "economic:" + json.dumps(fields)
    order = payload.get("broker_order")
    if isinstance(order, Mapping):
        order_id = order.get("broker_order_id")
        cumulative = order.get("filled_quantity")
        if isinstance(order_id, str) and order_id and cumulative is not None:
            quantity = _finite(cumulative)
            if quantity <= 0:
                raise ValueError("fill requires positive cumulative quantity")
            return "simulated:" + json.dumps([order_id, quantity])
    if isinstance(fallback, str) and fallback:
        return "transport:" + fallback
    raise ValueError("fill receipt identity unavailable")


def _rolling_average(previous: float, count: int, new_value: float) -> float:
    if count <= 0:
        return 0.0
    return previous + (new_value - previous) / max(1, count)


def _recompute_weight(stats: Mapping[str, Any]) -> float:
    avg_confidence = float(stats.get("avg_confidence") or 0.0)
    trades = float(stats.get("trades") or 0.0)
    pnl = float(stats.get("realized_pnl") or 0.0)
    penalties = float(stats.get("penalties") or 0.0)
    trade_bonus = min(0.5, trades / 40)
    pnl_bonus = max(-0.5, min(0.5, pnl / 10_000))
    penalty_drag = min(0.5, penalties * 0.1)
    weight = avg_confidence + trade_bonus + pnl_bonus - penalty_drag
    return round(max(0.1, min(2.5, weight)), 4)


def _apply_attribution(state: Dict[str, Any]) -> None:
    result = attribute_economic_envelopes(tuple(state["attribution_events"].values()))
    for stats in state["strategies"].values():
        stats["realized_pnl"] = 0.0
        stats["attributed_realized_pnl"] = "0"
    for name, value in result.realized_pnl.items():
        stats = state["strategies"].setdefault(name, _default_stats())
        stats["realized_pnl"] = float(value)
        stats["attributed_realized_pnl"] = str(value)
        stats["candidate_weight"] = _recompute_weight(stats)
    state["attribution_unavailable"] = list(result.unavailable_event_ids)


def _merge_attribution_event(
    state: Dict[str, Any], event_id: str, incoming: Dict[str, Any]
) -> None:
    prior = state["attribution_events"].get(event_id)
    if prior is None:
        state["attribution_events"][event_id] = incoming
        return
    if prior.get("economic_event") != incoming.get("economic_event"):
        raise ValueError("economic attribution identity conflict")
    old_owners = prior.get("strategies")
    new_owners = incoming.get("strategies")
    if old_owners is not None and new_owners is not None and old_owners != new_owners:
        raise ValueError("economic attribution identity conflict")
    if old_owners is None and new_owners is not None:
        prior["strategies"] = new_owners


def _candidate_version(
    strategy: str, strategy_hash: str, weight: object, *, safety_revision: int = 0
) -> str:
    value = {"strategy": strategy, "strategy_hash": strategy_hash, "candidate_weight": weight}
    if safety_revision:
        value["safety_revision"] = safety_revision
    return _fingerprint(value)
