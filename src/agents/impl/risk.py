"""Risk monitoring agent consuming market snapshots."""

from __future__ import annotations

import hashlib
import math
import os
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, List, Mapping, Sequence, cast

from observability.state import ObservabilityState
from ops.calendar import USTradingCalendar
from ops.reduction import ReductionPolicy, reduction_quantity
from ops.residual_reduction import (
    FractionalResidualCapability,
    FractionalResidualPolicy,
    authorize_fractional_residual,
)
from portfolio.postgres_store import JournalPortfolioStore
from portfolio.store import PortfolioSnapshot, PortfolioStore
from risk import StressTestHarness
from risk.estimates import DatedReturnHistory, RiskEstimate, estimate_var
from risk.service import RiskEvaluationService

from ..base import BaseAgent
from ..context import AgentContext
from ..messaging import Envelope, MessageBus, Subscription


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if math.isfinite(result):
        return result
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
        self.max_var_pct = float(os.environ.get("RISK_MAX_VAR_PCT", "0.04"))
        self._var_min_observations = 60
        self.max_drawdown_pct = float(os.environ.get("RISK_MAX_DRAWDOWN_PCT", "0.10"))
        self.drawdown_warning_pct = float(os.environ.get("RISK_DRAWDOWN_WARNING_PCT", "0.02"))
        self.nav_hard_stop_pct = float(os.environ.get("RISK_NAV_HARD_STOP_PCT", "0.05"))
        self.stop_loss_pct = float(os.environ.get("RISK_STOP_LOSS_PCT", "0.08"))
        self.stress_loss_threshold_pct = float(os.environ.get("RISK_STRESS_LOSS_PCT", "0.06"))
        self._stress_interval_ticks = int(os.environ.get("RISK_STRESS_TICK_INTERVAL", "12"))
        self._ticks_since_stress = 0
        self._nav_history: Deque[float] = deque(
            maxlen=int(os.environ.get("RISK_DRAWDOWN_WINDOW", "30"))
        )
        self._latest_prices: Dict[str, float] = {}
        self._active_stop_losses: set[str] = set()
        self._stress_harness = StressTestHarness()
        self._history_provider = extras.get("risk_history_provider")
        supplied_evaluator = extras.get("risk_evaluation_service")
        self._risk_evaluator = (
            supplied_evaluator if isinstance(supplied_evaluator, RiskEvaluationService) else None
        )
        supplied_now = extras.get("now")
        self._now: Callable[[], datetime] = (
            cast(Callable[[], datetime], supplied_now)
            if callable(supplied_now)
            else lambda: datetime.now(timezone.utc)
        )
        self._calendar = extras.get("risk_calendar") or USTradingCalendar()
        reduction_policy = extras.get("reduction_policy")
        self._reduction_policy = (
            reduction_policy if isinstance(reduction_policy, ReductionPolicy) else None
        )
        residual_policy = extras.get("fractional_residual_policy")
        self._fractional_residual_policy = (
            residual_policy if isinstance(residual_policy, FractionalResidualPolicy) else None
        )
        self._fractional_capability_provider = extras.get("fractional_residual_capability")

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
        symbol = raw_symbol.upper() if isinstance(raw_symbol, str) and raw_symbol.strip() else None
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
        self._evaluate_proposal(payload)

    def _evaluate_proposal(self, payload: Dict[str, Any]) -> None:
        raw_symbol = payload.get("symbol")
        symbol = raw_symbol.upper() if isinstance(raw_symbol, str) and raw_symbol.strip() else None
        price = _as_float(payload.get("price"))
        quantity = _as_float(payload.get("quantity"))
        proposal_id = payload.get("proposal_id")
        if (
            not symbol
            or price is None
            or price <= 0
            or quantity is None
            or quantity == 0
            or not isinstance(proposal_id, str)
            or not proposal_id.strip()
        ):
            return
        decision_id = payload.get("decision_id") or proposal_id
        if self._risk_evaluator is None:
            self._reject_with_reason(
                proposal_id,
                symbol,
                decision_id=decision_id,
                reason="risk_inputs_unavailable",
                strategies=payload.get("strategies"),
            )
            return
        try:
            artifact = self._risk_evaluator.freeze(
                proposal_id=str(proposal_id),
                symbol=symbol,
                side="buy" if quantity > 0 else "sell",
                quantity=abs(quantity),
                worst_price=price,
            )
        except Exception:
            self._reject_with_reason(
                proposal_id,
                symbol,
                decision_id=decision_id,
                reason="risk_inputs_unavailable",
                strategies=payload.get("strategies"),
            )
            return
        unified = artifact.decision
        if not unified.allowed:
            self._reject_with_reason(
                proposal_id,
                symbol,
                decision_id=decision_id,
                reason="unified_risk:" + unified.reasons[0],
                strategies=payload.get("strategies"),
            )
            return
        nav = float(unified.nav or 0)
        gross = float(unified.gross_notional or 0)
        leverage = gross / nav if nav > 0 else 0.0
        projected_exposures = {key: float(value) for key, value in unified.symbol_notionals.items()}
        is_reduction = artifact.candidate.side == "sell"
        estimate = self._estimate_daily_var(nav=nav, exposures=projected_exposures)
        if not estimate.available and not is_reduction:
            self._reject_with_reason(
                proposal_id,
                symbol,
                decision_id=decision_id,
                reason="risk_history_unavailable",
                extra={"history_reason": estimate.reason or "unavailable"},
                strategies=payload.get("strategies"),
            )
            return
        var_pct = estimate.var_fraction
        var_amount = var_pct * nav if var_pct is not None else None
        if var_pct is not None and var_pct > self.max_var_pct:
            assert var_amount is not None
            self._reject_with_reason(
                proposal_id,
                symbol,
                decision_id=decision_id,
                reason="var_limit",
                extra={"var_pct": round(var_pct, 4), "var_amount": round(var_amount, 2)},
            )
            return
        approvals = dict(payload.get("approvals") or {})
        approvals["risk"] = {
            "status": "approved",
            "timestamp": self._decision_time().isoformat(),
            "metrics": {
                "gross_exposure": gross,
                "leverage": leverage,
                "var_pct": var_pct,
                "var_available": estimate.available,
                "var_reason": estimate.reason,
            },
        }
        approval = {
            "proposal_id": proposal_id,
            "decision_id": decision_id,
            "symbol": symbol,
            "price": price,
            "quantity": quantity,
            "risk_limit": float(self._risk_evaluator.policy.max_single_name_fraction),
            "risk_artifact": {
                "candidate_hash": artifact.candidate_hash,
                "policy_hash": unified.policy_hash,
                "input_hash": unified.input_hash,
                "cutoff": artifact.cutoff.isoformat(),
                "expires_at": artifact.expires_at.isoformat(),
            },
            "approvals": approvals,
            "risk_metrics": {
                "nav": nav,
                "gross_exposure": gross,
                "leverage": leverage,
                "var_pct": var_pct,
                "var_amount": var_amount,
                "var_available": estimate.available,
                "var_reason": estimate.reason,
                "exposures": {
                    key: {
                        "value": value,
                        "pct_nav": value / nav if nav else 0.0,
                    }
                    for key, value in projected_exposures.items()
                },
            },
        }
        if "strategies" in payload:
            approval["strategies"] = payload.get("strategies")
        if "confidence" in payload:
            approval["confidence"] = payload.get("confidence")
        if "reduction_authorization" in payload:
            approval["reduction_authorization"] = payload.get("reduction_authorization")
            approval["reduction_client_order_id"] = payload.get("reduction_client_order_id")
        if "fractional_residual_authorization" in payload:
            approval["fractional_residual_authorization"] = payload.get(
                "fractional_residual_authorization"
            )
        self._update_observability(
            nav=nav,
            gross=gross,
            leverage=leverage,
            var_pct=var_pct,
            var_amount=var_amount,
        )
        self.bus.publish("risk.approval", payload=approval, publisher=self.name)
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
        decision_id: str | None = None,
        reason: str,
        extra: Mapping[str, float | int | str] | None = None,
        strategies: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "proposal_id": proposal_id,
            "decision_id": decision_id or proposal_id,
            "symbol": symbol,
            "reason": reason,
        }
        if extra:
            payload.update(extra)
        self.audit("risk_reject", payload)
        self.alert("risk_reject", payload, severity="error")
        if strategies:
            self._emit_strategy_feedback(strategies, reason=f"risk_{reason}", delta=-0.2)

    def _decision_time(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

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

    def _estimate_daily_var(
        self,
        *,
        nav: float,
        exposures: Mapping[str, float],
    ) -> RiskEstimate:
        try:
            now = self._now()
        except Exception:
            return RiskEstimate(False, None, "decision_clock_unavailable")
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return RiskEstimate(False, None, "invalid_decision_time")
        now = now.astimezone(timezone.utc)
        safe_nav = max(nav, 1.0)
        weights = {symbol: value / safe_nav for symbol, value in exposures.items() if safe_nav}
        symbols = tuple(
            sorted(symbol.strip().upper() for symbol, weight in weights.items() if weight)
        )
        if not symbols:
            return RiskEstimate(True, 0.0, None)
        provider_method = getattr(self._history_provider, "history", None)
        if not callable(provider_method):
            return RiskEstimate(False, None, "missing_history_provider")
        expected = self._latest_closed_sessions(now)
        if expected is None:
            return RiskEstimate(False, None, "calendar_unavailable")
        try:
            supplied = provider_method(symbols=symbols, as_of=now)
        except Exception:
            return RiskEstimate(False, None, "history_provider_failure")
        if not isinstance(supplied, DatedReturnHistory) or supplied.as_of != now:
            return RiskEstimate(False, None, "history_as_of_mismatch")
        normalized: dict[str, Mapping[date, float]] = {}
        for raw_symbol, series in supplied.returns.items():
            symbol = raw_symbol.strip().upper()
            if symbol in normalized:
                return RiskEstimate(False, None, "duplicate_history_symbol")
            normalized[symbol] = series
            for session in series:
                if type(session) is not date:
                    return RiskEstimate(False, None, "invalid_session_date")
                try:
                    bounds = self._calendar.session_bounds(session)
                except Exception:
                    return RiskEstimate(False, None, "calendar_unavailable")
                if bounds is None or bounds[1].astimezone(timezone.utc) > now:
                    return RiskEstimate(False, None, "noncausal_session_history")
        expected_set = set(expected)
        selected: dict[str, dict[date, float]] = {}
        for symbol in symbols:
            selected_series = normalized.get(symbol)
            if selected_series is None or not expected_set.issubset(selected_series):
                return RiskEstimate(False, None, "incomplete_venue_sessions")
            selected[symbol] = {session: selected_series[session] for session in expected}
        return estimate_var(selected, weights, self._var_min_observations)

    def _latest_closed_sessions(self, now: datetime) -> tuple[date, ...] | None:
        sessions: list[date] = []
        day = now.date()
        for _ in range(370):
            try:
                bounds = self._calendar.session_bounds(day)
            except Exception:
                return None
            if bounds is not None:
                closed = bounds[1]
                if closed.tzinfo is None or closed.utcoffset() is None:
                    return None
                if closed.astimezone(timezone.utc) <= now:
                    sessions.append(day)
                    if len(sessions) == self._var_min_observations:
                        return tuple(sorted(sessions))
            day -= timedelta(days=1)
        return None

    def _update_nav_history(self, snapshot: PortfolioSnapshot) -> None:
        nav = self._nav_from_snapshot(snapshot)
        self._nav_history.append(nav)
        if len(self._nav_history) < 2:
            return
        prev_nav = self._nav_history[-2]
        if prev_nav:
            day_change_pct = (nav - prev_nav) / prev_nav
            if day_change_pct <= -self.nav_hard_stop_pct:
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
        self.bus.publish("risk.kill_switch", payload=payload, publisher=self.name)
        self.alert("risk_kill_switch", payload, severity="critical")
        self.audit("risk_kill_switch", payload)
        strategies = details.get("strategies")
        if isinstance(strategies, list):
            self._emit_strategy_feedback(strategies, reason=reason, delta=-0.5)

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
            self.bus.publish("risk.stop_loss", payload=payload, publisher=self.name)
            self.alert("risk_stop_loss", payload, severity="error")
            self.audit("risk_stop_loss", payload)
            if self._reduction_policy is not None and self._risk_evaluator is not None:
                try:
                    quantity = reduction_quantity(position.quantity, self._reduction_policy)
                    # First-release submissions use whole shares. Round the proposed
                    # order down within the explicit cap; preserve ledger residuals.
                    quantity -= quantity % 1
                    residual_capability = None
                    if quantity <= 0:
                        residual_policy = self._fractional_residual_policy
                        provider = self._fractional_capability_provider
                        if residual_policy is None or not callable(provider):
                            return
                        candidate = provider(
                            account_id=str(getattr(self.portfolio_store, "account_id", "")),
                            mode=str(getattr(self.portfolio_store, "mode", "")),
                            symbol=symbol,
                            now=self._now,
                        )
                        if not isinstance(candidate, FractionalResidualCapability):
                            return
                        quantity = authorize_fractional_residual(
                            residual_policy,
                            candidate,
                            symbol=symbol,
                            quantity=str(position.quantity),
                            now=self._now(),
                        )
                        residual_capability = candidate
                except (ValueError, ArithmeticError):
                    return
                lifecycle = None
                if isinstance(self.portfolio_store, JournalPortfolioStore):
                    lifecycle = self.portfolio_store.journal.position_lifecycle_id(
                        self.portfolio_store.account_id, self.portfolio_store.mode, symbol
                    )
                stable_source = "|".join(
                    (
                        str(getattr(self.portfolio_store, "account_id", "simulated")),
                        str(getattr(self.portfolio_store, "mode", "simulated")),
                        symbol,
                        lifecycle or str(position.average_cost),
                        (
                            cast(
                                FractionalResidualPolicy, self._fractional_residual_policy
                            ).content_hash
                            if residual_capability is not None
                            else self._reduction_policy.content_hash
                        ),
                        str(self.stop_loss_pct),
                    )
                )
                proposal_id = "stop-" + hashlib.sha256(stable_source.encode()).hexdigest()
                if isinstance(self.portfolio_store, JournalPortfolioStore):
                    prior = self.portfolio_store.journal.intent_or_none(
                        self.portfolio_store.account_id,
                        self.portfolio_store.mode,
                        "reduction-" + proposal_id.removeprefix("stop-"),
                    )
                    if prior is not None:
                        return
                self._evaluate_proposal(
                    {
                        "proposal_id": proposal_id,
                        "decision_id": proposal_id,
                        "symbol": symbol,
                        "price": price,
                        "quantity": -float(quantity),
                        "reduction_client_order_id": "reduction-"
                        + proposal_id.removeprefix("stop-"),
                        "reduction_authorization": {
                            "policy_name": self._reduction_policy.name,
                            "policy_hash": self._reduction_policy.content_hash,
                            "quantity": str(quantity),
                        },
                        **(
                            {
                                "fractional_residual_authorization": {
                                    "policy_hash": cast(
                                        FractionalResidualPolicy,
                                        self._fractional_residual_policy,
                                    ).content_hash,
                                    "capability_checksum": residual_capability.checksum,
                                    "observed_at": residual_capability.observed_at.isoformat(),
                                }
                            }
                            if residual_capability is not None
                            else {}
                        ),
                    }
                )
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

    def _emit_strategy_feedback(
        self,
        strategies: Sequence[Mapping[str, Any]],
        *,
        reason: str,
        delta: float,
    ) -> None:
        for entry in strategies:
            name = entry.get("strategy")
            if not isinstance(name, str) or not name:
                continue
            payload = {
                "strategy": name,
                "delta": delta,
                "reason": reason,
                "timestamp": self._decision_time().isoformat(),
            }
            self.bus.publish("strategy.feedback", payload=payload, publisher=self.name)
            self.audit("strategy_feedback", payload)
