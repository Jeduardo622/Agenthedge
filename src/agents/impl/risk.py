"""Risk monitoring agent consuming market snapshots."""

from __future__ import annotations

import math
import os
import statistics
from collections import deque
from typing import Any, Deque, Dict, List, Mapping, Sequence

from observability.state import ObservabilityState
from portfolio.store import PortfolioSnapshot, PortfolioStore
from risk import StressTestHarness

from ..base import BaseAgent
from ..context import AgentContext
from ..messaging import Envelope, MessageBus, Subscription


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


class RiskAgent(BaseAgent):
    """Risk agent tracking price volatility and approving proposals."""

    def __init__(self, context: AgentContext) -> None:
        super().__init__(context)
        extras = context.extras or {}
        portfolio_store = extras.get("portfolio_store")
        if not isinstance(portfolio_store, PortfolioStore):
            raise RuntimeError("RiskAgent requires PortfolioStore in context extras")
        self.portfolio_store = portfolio_store
        observability_state = extras.get("observability_state")
        self._observability_state = (
            observability_state if isinstance(observability_state, ObservabilityState) else None
        )
        bus = context.message_bus
        if not bus:
            raise RuntimeError("RiskAgent requires a message bus")
        self.bus: MessageBus = bus
        self._subscriptions: List[Subscription] = []
        self._history: Dict[str, Deque[float]] = {}
        self._history_window = int(os.environ.get("RISK_PRICE_HISTORY", "60"))
        self._volatility_window = min(5, self._history_window)
        self._threshold_pct = float(os.environ.get("RISK_VOL_THRESHOLD_PCT", "5.0"))
        self.max_position_pct = float(os.environ.get("RISK_MAX_POSITION_PCT", "0.1"))
        self.max_var_pct = float(os.environ.get("RISK_MAX_VAR_PCT", "0.04"))
        self.var_lookback = int(os.environ.get("RISK_VAR_LOOKBACK", "20"))
        self.var_confidence = float(os.environ.get("RISK_VAR_CONFIDENCE", "0.95"))
        self.max_drawdown_pct = float(os.environ.get("RISK_MAX_DRAWDOWN_PCT", "0.10"))
        self.drawdown_warning_pct = float(os.environ.get("RISK_DRAWDOWN_WARNING_PCT", "0.02"))
        self.nav_hard_stop_pct = float(os.environ.get("RISK_NAV_HARD_STOP_PCT", "0.05"))
        self.stop_loss_pct = float(os.environ.get("RISK_STOP_LOSS_PCT", "0.08"))
        self.max_leverage = float(os.environ.get("RISK_MAX_GROSS_LEVERAGE", "1.2"))
        self.stress_loss_threshold_pct = float(os.environ.get("RISK_STRESS_LOSS_PCT", "0.06"))
        self._stress_interval_ticks = int(os.environ.get("RISK_STRESS_TICK_INTERVAL", "12"))
        self._ticks_since_stress = 0
        self._nav_history: Deque[float] = deque(
            maxlen=int(os.environ.get("RISK_DRAWDOWN_WINDOW", "30"))
        )
        self._latest_prices: Dict[str, float] = {}
        self._active_stop_losses: set[str] = set()
        self._stress_harness = StressTestHarness()

    @property
    def _var_zscore(self) -> float:
        mapping = {
            0.90: 1.28,
            0.95: 1.65,
            0.975: 1.96,
            0.99: 2.33,
        }
        return mapping.get(round(self.var_confidence, 3), 1.65)

    def setup(self) -> None:
        self._subscriptions.append(
            self.bus.subscribe(self._handle_snapshot, topics=["market.snapshot"], replay_last=5)
        )
        self._subscriptions.append(
            self.bus.subscribe(self._handle_proposal, topics=["quant.proposal"], replay_last=0)
        )

    def teardown(self) -> None:
        for subscription in self._subscriptions:
            self.bus.unsubscribe(subscription.id)
        self._subscriptions = []

    def tick(self) -> None:
        self.publish_metric("risk_symbols_tracked", float(len(self._history)))
        self._maybe_run_stress_test()

    def _handle_snapshot(self, envelope: Envelope) -> None:
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        raw_symbol = payload.get("symbol")
        symbol = str(raw_symbol) if raw_symbol is not None else None
        latest_close = _as_float(payload.get("latest_close"))
        if not symbol or latest_close is None:
            return
        history = self._history.setdefault(symbol, deque(maxlen=self._history_window))
        history.append(latest_close)
        self._latest_prices[symbol] = latest_close
        snapshot = self.portfolio_store.snapshot()
        self._check_stop_loss(symbol, latest_close, snapshot)
        self._update_nav_history(snapshot)
        if len(history) >= 2:
            prev = history[-2]
            change_pct = ((history[-1] - prev) / prev) * 100 if prev else 0
            if len(history) >= self._volatility_window and abs(change_pct) >= self._threshold_pct:
                self.logger.warning("volatility alert for %s: %.2f%% change", symbol, change_pct)
                payload = {"symbol": symbol, "change_pct": round(change_pct, 2)}
                self.audit("risk_alert", payload)
                self.alert("risk_alert", payload, severity="warning")

    def _handle_proposal(self, envelope: Envelope) -> None:
        payload: Dict[str, Any] = dict(envelope.message.payload or {})
        raw_symbol = payload.get("symbol")
        symbol = str(raw_symbol) if raw_symbol is not None else None
        price = _as_float(payload.get("price"))
        quantity = _as_float(payload.get("quantity"))
        proposal_id = payload.get("proposal_id")
        if not symbol or price is None or quantity is None or not proposal_id:
            return
        snapshot = self.portfolio_store.snapshot()
        exposures = self._build_exposure_table(snapshot)
        projected_exposures = dict(exposures)
        projected_exposures[symbol] = projected_exposures.get(symbol, 0.0) + quantity * price
        nav = self._nav_from_snapshot(snapshot)
        gross = sum(abs(value) for value in projected_exposures.values())
        leverage = gross / max(nav, 1.0)
        notional = abs(quantity * price)
        limit = max(1.0, snapshot.cash * self.max_position_pct)
        if notional > limit:
            self.logger.warning(
                "risk rejected proposal %s for %s (notional %.2f > limit %.2f)",
                proposal_id,
                symbol,
                notional,
                limit,
            )
            payload = {"proposal_id": proposal_id, "symbol": symbol, "reason": "notional_limit"}
            self.audit("risk_reject", payload)
            self.alert("risk_reject", payload, severity="error")
            return
        if leverage > self.max_leverage:
            self._reject_with_reason(
                proposal_id,
                symbol,
                reason="gross_leverage_limit",
                extra={"projected_leverage": round(leverage, 3)},
            )
            return
        var_amount, var_pct = self._estimate_portfolio_var(
            nav=nav,
            exposures=projected_exposures,
        )
        if var_pct > self.max_var_pct:
            self._reject_with_reason(
                proposal_id,
                symbol,
                reason="var_limit",
                extra={"var_pct": round(var_pct, 4), "var_amount": round(var_amount, 2)},
            )
            return
        approval = {
            "proposal_id": proposal_id,
            "symbol": symbol,
            "price": price,
            "quantity": quantity,
            "risk_limit": limit,
            "risk_metrics": {
                "nav": nav,
                "gross_exposure": gross,
                "leverage": leverage,
                "var_pct": var_pct,
                "var_amount": var_amount,
                "exposures": {
                    key: {
                        "value": value,
                        "pct_nav": value / nav if nav else 0.0,
                    }
                    for key, value in projected_exposures.items()
                },
            },
        }
        self._update_observability(
            nav=nav,
            gross=gross,
            leverage=leverage,
            var_pct=var_pct,
            var_amount=var_amount,
        )
        self.bus.publish("risk.approval", payload=approval)
        self.publish_metric("risk_approved", 1.0, {"symbol": symbol})

    def _maybe_run_stress_test(self) -> None:
        self._ticks_since_stress += 1
        if self._ticks_since_stress < self._stress_interval_ticks:
            return
        self._ticks_since_stress = 0
        snapshot = self.portfolio_store.snapshot()
        nav = self._nav_from_snapshot(snapshot)
        exposures = self._build_exposure_table(snapshot)
        results = self._stress_harness.run(exposures, nav=nav)
        breached = [result for result in results if result.breached(self.stress_loss_threshold_pct)]
        payload = {
            "nav": nav,
            "stress_results": self._stress_harness.as_dict(results),
            "threshold_pct": self.stress_loss_threshold_pct,
        }
        self.audit("risk_stress_run", payload)
        self._update_observability(
            nav=nav,
            gross=sum(abs(value) for value in exposures.values()),
            leverage=self._compute_leverage(nav, exposures),
            stress=payload,
        )
        if breached:
            worst = min(breached, key=lambda result: result.pnl_pct)
            self.alert(
                "risk_stress_breach",
                {
                    **payload,
                    "worst_scenario": worst.scenario.name,
                    "pnl_pct": worst.pnl_pct,
                },
                severity="critical",
            )
            self._emit_kill_switch(
                reason=f"stress_breach:{worst.scenario.name}",
                details={
                    "pnl_pct": worst.pnl_pct,
                    "nav": nav,
                },
            )

    def _reject_with_reason(
        self,
        proposal_id: str,
        symbol: str,
        *,
        reason: str,
        extra: Mapping[str, float | int | str] | None = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "proposal_id": proposal_id,
            "symbol": symbol,
            "reason": reason,
        }
        if extra:
            payload.update(extra)
        self.audit("risk_reject", payload)
        self.alert("risk_reject", payload, severity="error")

    def _build_exposure_table(self, snapshot: PortfolioSnapshot) -> Dict[str, float]:
        exposures: Dict[str, float] = {}
        for symbol, position in snapshot.positions.items():
            price = self._latest_prices.get(symbol, position.average_cost)
            exposures[symbol] = position.quantity * price
        return exposures

    def _nav_from_snapshot(self, snapshot: PortfolioSnapshot) -> float:
        exposures = self._build_exposure_table(snapshot)
        nav = float(snapshot.cash + sum(exposures.values()))
        return nav

    def _estimate_portfolio_var(
        self,
        *,
        nav: float,
        exposures: Mapping[str, float],
    ) -> tuple[float, float]:
        safe_nav = max(nav, 1.0)
        weights = {symbol: value / safe_nav for symbol, value in exposures.items() if safe_nav}
        variance = 0.0
        for symbol, weight in weights.items():
            returns = self._symbol_returns(symbol)
            if len(returns) < 2:
                continue
            try:
                symbol_var = statistics.pvariance(returns)
            except statistics.StatisticsError:
                continue
            variance += (weight**2) * symbol_var
        if variance <= 0.0:
            return 0.0, 0.0
        std_dev = math.sqrt(variance)
        var_amount = self._var_zscore * std_dev * safe_nav
        return var_amount, var_amount / safe_nav

    def _symbol_returns(self, symbol: str) -> Sequence[float]:
        history = self._history.get(symbol)
        if not history or len(history) < 2:
            return []
        values = list(history)
        tail_len = min(len(values), self.var_lookback + 1)
        tail = values[-tail_len:]
        returns: List[float] = []
        for prev, curr in zip(tail, tail[1:]):
            if prev:
                returns.append((curr - prev) / prev)
        return returns

    def _update_nav_history(self, snapshot: PortfolioSnapshot) -> None:
        nav = self._nav_from_snapshot(snapshot)
        self._nav_history.append(nav)
        if len(self._nav_history) < 2:
            return
        prev_nav = self._nav_history[-2]
        if prev_nav:
            day_change_pct = (nav - prev_nav) / prev_nav
            if abs(day_change_pct) >= self.nav_hard_stop_pct:
                self._emit_kill_switch(
                    reason="daily_loss_hard_stop",
                    details={"daily_change_pct": day_change_pct, "nav": nav},
                )
                return
        peak = max(self._nav_history)
        if not peak:
            return
        drawdown_pct = (nav - peak) / peak
        self._update_observability(nav=nav, drawdown_pct=drawdown_pct)
        if abs(drawdown_pct) >= self.max_drawdown_pct:
            self.alert(
                "risk_drawdown_warning",
                {"drawdown_pct": drawdown_pct, "nav": nav},
                severity="warning",
            )
        elif abs(drawdown_pct) >= self.drawdown_warning_pct:
            self.alert(
                "risk_drawdown_soft",
                {"drawdown_pct": drawdown_pct, "nav": nav},
                severity="info",
            )

    def _emit_kill_switch(self, *, reason: str, details: Mapping[str, Any]) -> None:
        payload = {"reason": reason, **details}
        self.bus.publish("risk.kill_switch", payload=payload)
        self.alert("risk_kill_switch", payload, severity="critical")
        self.audit("risk_kill_switch", payload)

    def _check_stop_loss(
        self,
        symbol: str,
        price: float,
        snapshot: PortfolioSnapshot,
    ) -> None:
        position = snapshot.positions.get(symbol)
        if not position or position.quantity == 0.0:
            self._active_stop_losses.discard(symbol)
            return
        direction = 1 if position.quantity > 0 else -1
        if position.average_cost <= 0:
            return
        move_pct = ((price - position.average_cost) / position.average_cost) * 100 * direction
        if move_pct <= -(self.stop_loss_pct * 100):
            if symbol in self._active_stop_losses:
                return
            self._active_stop_losses.add(symbol)
            payload = {
                "symbol": symbol,
                "price": price,
                "average_cost": position.average_cost,
                "quantity": position.quantity,
                "loss_pct": move_pct,
            }
            self.bus.publish("risk.stop_loss", payload=payload)
            self.alert("risk_stop_loss", payload, severity="error")
            self.audit("risk_stop_loss", payload)
        else:
            self._active_stop_losses.discard(symbol)

    def _compute_leverage(self, nav: float, exposures: Mapping[str, float]) -> float:
        gross = sum(abs(value) for value in exposures.values())
        safe_nav = max(nav, 1.0)
        return gross / safe_nav

    def _update_observability(
        self,
        *,
        nav: float | None = None,
        gross: float | None = None,
        leverage: float | None = None,
        var_pct: float | None = None,
        var_amount: float | None = None,
        drawdown_pct: float | None = None,
        stress: Mapping[str, Any] | None = None,
    ) -> None:
        if not self._observability_state:
            return
        payload: Dict[str, Any] = {}
        if nav is not None:
            payload["nav"] = nav
        if gross is not None:
            payload["gross_exposure"] = gross
        if leverage is not None:
            payload["leverage"] = leverage
        if var_pct is not None:
            payload["var_pct"] = var_pct
        if var_amount is not None:
            payload["var_amount"] = var_amount
        if drawdown_pct is not None:
            payload["drawdown_pct"] = drawdown_pct
        if stress is not None:
            payload["last_stress_run"] = stress
        if payload:
            self._observability_state.update_risk(payload)
