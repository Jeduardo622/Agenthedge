"""Performance tracker for adaptive strategy weighting."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_stats() -> Dict[str, Any]:
    return {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "realized_pnl": 0.0,
        "avg_confidence": 0.0,
        "penalties": 0,
        "weight": 1.0,
        "last_updated": _now(),
    }


class PerformanceTracker:
    """Persists per-strategy metrics and derives adaptive weights."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        state = self._load()
        self._strategies: Dict[str, Dict[str, Any]] = state.get("strategies", {})
        self._last_realized_pnl: float | None = state.get("last_realized_pnl")

    def record_fill(self, payload: Mapping[str, Any]) -> None:
        strategies = payload.get("strategies") or []
        if not isinstance(strategies, list) or not strategies:
            return
        portfolio = payload.get("portfolio") or {}
        realized_pnl = portfolio.get("realized_pnl")
        pnl_delta = None
        if isinstance(realized_pnl, (int, float)):
            if self._last_realized_pnl is None:
                pnl_delta = 0.0
            else:
                pnl_delta = float(realized_pnl) - self._last_realized_pnl
            self._last_realized_pnl = float(realized_pnl)
        else:
            pnl_delta = 0.0
        share_delta = pnl_delta / len(strategies) if strategies else 0.0
        with self._lock:
            for strategy_entry in strategies:
                name = strategy_entry.get("strategy")
                if not isinstance(name, str) or not name:
                    continue
                confidence = strategy_entry.get("confidence")
                confidence_value = (
                    float(confidence) if isinstance(confidence, (int, float)) else 0.0
                )
                stats = self._strategies.setdefault(name, _default_stats())
                stats["trades"] += 1
                if share_delta > 0:
                    stats["wins"] += 1
                elif share_delta < 0:
                    stats["losses"] += 1
                stats["realized_pnl"] += share_delta
                stats["avg_confidence"] = _rolling_average(
                    stats["avg_confidence"], stats["trades"], confidence_value
                )
                stats["last_updated"] = _now()
                stats["weight"] = _recompute_weight(stats)
        self._persist()

    def apply_feedback(self, strategy: str, delta: float, reason: str | None = None) -> None:
        if not strategy:
            return
        with self._lock:
            stats = self._strategies.setdefault(strategy, _default_stats())
            if delta < 0:
                stats["penalties"] += 1
            stats["weight"] = round(max(0.1, min(2.5, stats["weight"] + delta)), 4)
            stats["last_feedback"] = {
                "reason": reason,
                "delta": delta,
                "timestamp": _now(),
            }
            stats["last_updated"] = _now()
        self._persist()

    def snapshot(self) -> Mapping[str, Mapping[str, Any]]:
        with self._lock:
            return {name: dict(stats) for name, stats in self._strategies.items()}

    def weights(self) -> Dict[str, float]:
        with self._lock:
            return {name: stats.get("weight", 1.0) for name, stats in self._strategies.items()}

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "strategies": self._strategies,
                "last_realized_pnl": self._last_realized_pnl,
            }

    def _load(self) -> MutableMapping[str, Any]:
        if not self._path.exists():
            return {"strategies": {}, "last_realized_pnl": None}
        try:
            data = json.loads(self._path.read_text())
        except json.JSONDecodeError:
            return {"strategies": {}, "last_realized_pnl": None}
        if not isinstance(data, MutableMapping):
            return {"strategies": {}, "last_realized_pnl": None}
        strategies = data.get("strategies")
        if not isinstance(strategies, MutableMapping):
            strategies = {}
        return {
            "strategies": {str(name): dict(stats) for name, stats in strategies.items()},
            "last_realized_pnl": data.get("last_realized_pnl"),
        }

    def _persist(self) -> None:
        self._path.write_text(json.dumps(self.to_dict(), indent=2))


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
