# Agenthedge Risk and Data Completion Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Execute checkbox steps after fresh classification.

**Goal:** Make approved risk limits effective across orders, sessions, stale feeds and restarts.

**Architecture:** One versioned policy, one marked portfolio view including reservations, persisted session risk, canonical timestamped inputs and a session calendar adapter.

**Tech Stack:** Python, Decimal, PostgreSQL, pytest; separately pinned exchange_calendars if qualified.

**Spec:** [Research/design](../specs/2026-09-14-platform-completion-research.md); [master contracts](2026-09-14-platform-completion.md).

## Global constraints

All master constraints apply. Thresholds in the design are proposed and must be accepted before real-money activation. Preserve Compliance veto and approved reduction paths. Reviewer: risk/data specialist; tester: aggregate-order, session and fault invariants. No real provider calls in regression tests.

## R1 — Unified policy, valuation and pending reservations

**Create:** `src/risk/policy.py`, `src/risk/valuation.py`, `tests/risk/test_policy.py`, `tests/risk/test_valuation.py`, `config/risk/etf-sector-map.json`. **Modify:** `src/agents/impl/risk.py`, `src/agents/impl/compliance.py`, `src/portfolio/safety.py`, `src/agents/config.py`. **Depends:** B1, integrates E4 reservations. **Produces:** RiskPolicy/projected_exposure defined in master.

- [ ] Write policy validation and marked-exposure tests. Reject NaN/infinity/negative limits and ambiguous policy defaults. Include the repeated-order bypass:

```python
def test_existing_and_reserved_position_count():
    from decimal import Decimal as D
    from risk.valuation import WorkingOrderReservation, projected_exposure
    result = projected_exposure(positions={"SPY": D("90")},
        reservations=(WorkingOrderReservation("o1", "SPY", "buy", D("15"), D("100"), D("1500"), "accepted"),),
        symbol="SPY", delta=D("5"),
        marks={"SPY": D("100")}, cash=D("91000"))
    assert result["nav"] == D("100000")
    assert result["symbol_notional"] == D("11000")
    assert result["symbol_notional"] / result["nav"] > D("0.10")
```

- [ ] Run `poetry run pytest tests/risk/test_policy.py tests/risk/test_valuation.py -v`; record initial missing-behavior failures.
- [ ] Implement a single signed marked-NAV calculation and immutable policy hash. Reserve worst-case buying power/exposure atomically before submission. Preserve each working order's identity, side, remaining quantity, reserved buying power, cancellation state and worst price; do not net opposing orders that may fill independently. Evaluate worst-case reachable exposure and cash use across fill sequences. Release reservations only on confirmed fills/cancellations. Enforce single-name, sector, gross exposure, cash/buying power and approved liquidity limits. ETF sector exposure uses an approved as-of look-through weight map (sum=1, source/date/hash, maximum age in policy); do not assign SPY/QQQ to a fictitious single sector. Missing or stale mappings/volume block increased exposure; synthetic test weights are not deployed holdings data.
- [ ] Run risk/compliance/broker tests. Verify low-cash reduction allowed, overselling treated as new short exposure, concurrent approvals cannot oversubscribe, and Risk/Compliance report the same NAV/policy hash. Include simultaneous buy/sell orders where only the buy fills; netting must not conceal a breach. Include overlapping sell reservations exceeding held shares and ETF+direct-stock sector exposure using synthetic weights. First-release scope rejects new shorts, margin and unsupported assets explicitly.
- [ ] Reviewer checks conflicts against existing documents; commit policy and integrations with revised documented behavior. Do not silently lower protection to obtain passing tests.

## R2 — Durable session loss, drawdown and stop policy

**Create:** `src/risk/session.py`, `tests/risk/test_session.py`, `tests/integration/test_session_risk.py`. **Modify:** `src/agents/impl/risk.py`, `src/infra/postgres.py`, `src/agents/runtime.py`. **Depends:** R1, R5 calendar; integrates E6. **Produces:** SessionRiskState/session_return in master.

- [ ] Add the gradual-loss regression plus deposits/withdrawals, restart and new-session fixtures:

```python
def test_session_loss_uses_opening_equity():
    from decimal import Decimal as D
    from risk.session import SessionRiskState, session_return
    state = SessionRiskState("XNYS:2026-09-14", D("100000"), D("0"), False)
    assert session_return(state, D("94119.2")) == D("-0.058808")
```

- [ ] Run `poetry run pytest tests/risk/test_session.py -v`; add an integration assertion that the full tick sequence persists a halt and blocks subsequent new orders after restart.
- [ ] Persist baseline, external flows, rolling session marks, policy hash and active halt. Define return as `(equity - external_flows - opening_equity) / opening_equity`; reject nonpositive baseline. Record a new baseline only at the defined session boundary with fresh valuation. New-session rollover does not clear an unresolved safety incident. Apply configured pause/hard-halt/drawdown actions through E6.
- [ ] Run session/risk/control tests including no-trade days, holidays, cashflows near rollover, repeated restart, stop-loss rejection and a price gap. Verify rolling windows count sessions, not ticks. Require a dedicated PostgreSQL test for persistence and concurrency.
- [ ] Reviewer approves economic interpretation and tester verifies actual order prevention, not just emitted messages. Commit the focused session-control change.

## R3 — Freshness, provenance and canonical inputs

**Create:** `src/data/snapshot.py`, `tests/data/test_snapshot.py`. **Modify:** `src/data/quality.py`, `src/data/ingestion/service.py`, relevant `src/data/providers/` adapters, `src/agents/impl/director.py`, `src/portfolio/safety.py`, `tests/data/test_quality.py`. **Depends:** B1; contract consumed by S1. **Produces:** CanonicalSnapshot in master.

- [ ] Add quote validation regressions using an injectable `now` in the proposed quality API:

```python
def test_ancient_quote_fails():
    from datetime import datetime, timezone
    from data.quality import DataQualityChecker
    issues = DataQualityChecker(quote_freshness_seconds=1).check_quote(
        {"c": 100, "pc": 100, "t": 1},
        now=datetime(2026, 9, 14, 15, tzinfo=timezone.utc))
    assert any(x.reason == "stale_quote" for x in issues)
```

- [ ] Run `poetry run pytest tests/data/test_quality.py -k ancient_quote -v`; initially the absent now parameter/freshness behavior fails. Add missing/future timestamps, NaN/infinity/zero/negative values and stale cached fallbacks.
- [ ] Normalize provider timestamps without replacing event time with fetch time. Produce source/revision/checksum and availability time. Preserve timestamps through cache. Enforce required input validity before directive and immediately before submit; a stale approval cannot authorize an order later. Pass validated news/fundamentals to strategies; mark each missing dependency as non-participation rather than invented data.
- [ ] Run data/director/strategy/broker tests with all-provider failure, rate limits, partial recovery and permission-denied feeds. Verify errors are redacted and optional strategy disablement does not manufacture quorum. A price-only fallback must be explicitly present in the accepted strategy configuration.
- [ ] Reviewer verifies provider field semantics against official documentation at implementation time; commit canonical inputs and bounded adapters, preserving unrelated ingestion behavior.

## R4 — Warm-up and statistically defined risk estimates

**Create:** `src/risk/estimates.py`, `tests/risk/test_estimates.py`. **Modify:** `src/agents/impl/risk.py`, `src/risk/stress.py`, risk tests. **Depends:** R1/R3. **New interface:** `estimate_var(returns: dict[str, dict[date, float]], weights: dict[str, float], min_observations: int) -> RiskEstimate`, frozen result fields `available: bool`, `var_fraction: float | None`, `reason: str | None`. Date keys are venue session dates for close-to-close daily returns, not unordered tuple positions; preserve missing-session detection before intersection/alignment.

- [ ] Write no-history and correlated-return cases:

```python
def test_no_history_is_unavailable():
    from risk.estimates import estimate_var
    result = estimate_var(returns={}, weights={"SPY": .9}, min_observations=60)
    assert result.available is False
    assert result.var_fraction is None
```

- [ ] Run `poetry run pytest tests/risk/test_estimates.py -v`; add missing-symbol/aligned-date checks, equal-length series with different missing session dates, insufficient history and non-finite series before implementation.
- [ ] Define daily return observations and one-day 95% VaR for this mandate. Require at least 60 aligned observations as the proposed minimum; calculate portfolio returns/covariance consistently and preserve stress scenarios as a separate gate. Model warm-up as unavailable, blocking new risk and allowing authorized reductions. Do not annualize intraday tick variance as daily risk.
- [ ] Verify two identical assets do not falsely diversify risk away, singular covariance and constant series remain distinguishable from missing data, and high stress loss acts through E6. Repeat using canonical replay snapshots and real runtime fixture inputs.
- [ ] Reviewer checks the estimate's mathematical meaning and limits; commit the estimator and risk integration. Passing VaR alone does not establish tail safety.

## R5 — Session-aware scheduling and clock consistency

**Modify:** `src/ops/calendar.py`, `src/ops/scheduler.py`, `tests/ops/test_calendar.py`, `tests/ops/test_scheduler.py`; qualify dependency in `pyproject.toml`/`poetry.lock` separately. **Depends:** B1. **New interface:** `USTradingCalendar.session_bounds(day: date) -> tuple[datetime, datetime] | None` with UTC-aware open/close.

- [ ] Add tests for weekends, exchange holidays, early closes and DST offsets, plus an unavailable calendar. Avoid making a test date depend on today's clock:

```python
def test_regular_session_utc_bounds():
    from datetime import date, datetime, timezone
    from ops.calendar import USTradingCalendar
    assert USTradingCalendar().session_bounds(date(2026, 9, 14)) == (
        datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc),
        datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc))
```

- [ ] Run `poetry run pytest tests/ops/test_calendar.py tests/ops/test_scheduler.py -v` and capture missing session-bound behavior.
- [ ] Replace fixed wall-clock trading times with session-relative actions and a broker-clock check. Keep preflight distinct from order submission. Missed starts/restarts reconcile first; a job does not submit merely because the date is a trading day. Refuse order start on unresolved local/broker clock disagreement.
- [ ] Verify exactly one account worker owns the session, early-close closeout is correctly scheduled, pre-open checks place no orders, and restart cannot repeat already completed jobs. Compare representative calendar dates with broker calendar before broker qualification.
- [ ] Reviewer checks scheduling/fencing and dependency compatibility; commit calendar adapter and scheduler changes.

Reference: [pinned exchange_calendars XNYS implementation](https://github.com/gerrymanoim/exchange_calendars/blob/1eabe9da1f7b159dda12284e8a684f76f6323523/exchange_calendars/exchange_calendar_xnys.py). An exchange calendar is a planning schedule, not a substitute for a current broker market-clock check.
