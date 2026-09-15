# Agenthedge Completion Research and Design

## Recommendation

Complete Agenthedge through a sequence of independently verifiable releases. Retain its Python agent orchestration, broker boundary, PostgreSQL infrastructure, and dashboard. Repair the economic accounting and safety contracts first, then make historical evaluation and live operation consume the same decision inputs. Treat strategy acceptance as a separate gate from software completion.

The recommended approach is selective adoption of established methods. NautilusTrader provides a useful execution/reconciliation reference; Alpaca's official SDK and API documentation define broker behavior; LEAN provides realistic fill-model examples; Freqtrade provides bias-detection methods; exchange_calendars provides session schedules; Hypothesis provides generated action-sequence testing. None is a turnkey replacement for the platform's specific governance and operational requirements.[^1][^2][^3][^4][^5][^6]

This design targets a first complete release for one owner-operated account. It proposes US-listed equities and ETFs, USD accounting, whole-share long positions, regular market hours, and a small explicit symbol allowlist. These are planning assumptions that simplify first-release qualification, not newly approved trading settings. Shorting, options, futures, FX, crypto, multi-account customer service and fully self-modifying strategies remain named expansion milestones. The original multi-asset specification must not be marked complete after the first equities release.

The whole-share rule applies to new-risk order submissions. The ledger must still represent fractional residuals caused by corporate actions and cash-in-lieu. A broker-supported, explicitly authorized residual-reduction route handles those holdings; rounding them away is prohibited.

## Baseline and completion definitions

The September 14 audit evaluated commit `9426494ffa368f43f7d8c4cd6aee11588e5e0385`. It recorded 569 passing tests, 11 locally skipped PostgreSQL tests, 85.44% coverage, passing lint/type/build checks, and passing exact-commit CI. It also reproduced economic-accounting errors and risk-control defects. The application has an Alpaca live adapter already; the principal work is making its behavior reliable, observable and supported by current evidence.[^7]

Three definitions of completion are necessary:

1. **Software complete:** the supported instrument/account scope works end to end, documented safeguards act on actual runtime/broker state, accounting survives restarts, and operators can diagnose and recover failures.
2. **Release accepted:** the exact code/configuration/data-policy combination passes historical, fault-injection, broker-paper and operational acceptance gates; the owner accepts the proposed live mandate.
3. **Strategy validated:** a frozen strategy demonstrates acceptable risk and net performance under an explicit evaluation protocol. The system can be software complete while no strategy qualifies for capital. Rejected strategies must produce an honest hold decision.

The initial release is finished only when an operator can install it, run an isolated simulation, inspect data readiness, start a paper session, inspect orders/fills/P&L, halt activity, reconcile late fills, recover after restart, close the session, and export a release dossier. The supervised live route must use the same verified controls. A dashboard screenshot or generated approval packet alone does not satisfy these workflows.

## Architecture alternatives

| Approach | Benefits | Cost and risk | Decision |
| --- | --- | --- | --- |
| Strengthen existing architecture with selective dependencies | Preserves tested agent/governance work; each repair is independently reviewable | Requires explicit journal, risk, clock and accounting contracts | **Recommended** |
| Move the execution/replay engine to NautilusTrader | Unified event-driven research/live architecture and substantial execution machinery | Adapter, portfolio, lifecycle, state, Rust/Python and release migration; current upstream branch must be qualified | Keep as an alternative if a bounded spike proves materially lower total work |
| Replatform onto LEAN | Established equity research and reality-modeling ecosystem | C#/.NET engine and different algorithm model; rework governance, persistence and operator integration | Use as an optional independent reference runner before considering migration |

Upstream descriptions establish features, not migration effort. The cost comparison is an engineering judgment based on the current Agenthedge boundaries. A replacement spike is capped at two engineer-days, uses synthetic fixtures, and must demonstrate input parity, a partial-fill lifecycle, restart reconciliation and a documented integration map. If it cannot do so, retain the recommended architecture. No benchmark or popularity metric substitutes for these acceptance results.

## Repository research

The following snapshots were observed on September 14, 2026. They are research references, not qualified dependency versions. Branch-head code can differ from a released package; every adopted package must receive a pinned release, lockfile, license review and Windows/Linux compatibility test.

| Repository | Observed commit | Useful method | Adoption boundary |
| --- | --- | --- | --- |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader/tree/5dec1b07c00a6af6dec3a943b4ce473dbb276457) | `5dec1b07c00a6af6dec3a943b4ce473dbb276457` | Separate order/fill state and reconciliation; common event-driven simulation/live model | Design reference first; no wholesale runtime import |
| [alpacahq/alpaca-py](https://github.com/alpacahq/alpaca-py/tree/48fd544334a595c53e386043cc9282824b1e9c58) | `48fd544334a595c53e386043cc9282824b1e9c58` | Typed orders, trade stream, order lookup and cancellation | Qualify a thin adapter behind existing BrokerAdapter |
| [QuantConnect/Lean](https://github.com/QuantConnect/Lean/tree/02e491cc4b2fb6b09a2a8c0b82b64243bbfa7d76) | `02e491cc4b2fb6b09a2a8c0b82b64243bbfa7d76` | Explicit fill, fee and slippage models, warm-up behavior | Modeling reference and optional differential replay |
| [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade/tree/eec4eb074bd919d405fb60be8eaee49d3a49b511) | `eec4eb074bd919d405fb60be8eaee49d3a49b511` | Lookahead and initialization-sensitivity analysis | Reimplement Agenthedge-specific validation from documented methods |
| [gerrymanoim/exchange_calendars](https://github.com/gerrymanoim/exchange_calendars/tree/1eabe9da1f7b159dda12284e8a684f76f6323523) | `1eabe9da1f7b159dda12284e8a684f76f6323523` | Sessions, opens/closes, exceptional holidays and early closes | Narrow calendar adapter, compared with broker clock/calendar |
| [HypothesisWorks/hypothesis](https://github.com/HypothesisWorks/hypothesis) | Documentation reviewed; package version not yet qualified | Generate action sequences and assert state invariants | Development dependency only |

LEAN, alpaca-py and exchange_calendars identify Apache-2.0 licensing; NautilusTrader identifies LGPL-3.0; Freqtrade identifies GPL-3.0; Hypothesis identifies MPL-2.0. Agenthedge currently has an MIT license. These labels are source facts, not a legal compatibility determination. Prefer using documented ideas and isolated tools; inspect the exact license and notices before copying code or distributing dependencies. No code was imported for this design.[^1][^2][^3][^4][^5][^6]

### Broker events and economic truth

Alpaca distinguishes partial fills, final fills, cancellation requests and completed cancellations. Its trade stream exposes execution events, while account activities provide historical economic activity. Agenthedge should consume streaming events for timeliness and reconcile against REST history after reconnects. A stream connection is not proof that no messages were missed.[^8][^9][^10]

Adopt an immutable economic-event journal with a unique account/broker-event identity. Maintain order state, fill state and delivery checkpoints separately. An order's terminal status must never mean that all economic effects have been posted. Store submission intent before the request, persist a deterministic client order ID, and recover ambiguous requests by lookup instead of submitting a new identity.

Use decimal quantities/prices/notional values at the ledger boundary. If only cumulative order fills are available, incremental quantity is new cumulative quantity minus posted quantity, and incremental value is new cumulative value minus posted value. Divide these deltas only when the quantity delta is positive. A changed value with unchanged quantity is a correction to reconcile, not an ordinary new fill. Prefer broker execution/activity identifiers when available, and stop on inconsistent identities.

PostgreSQL can enforce unique event identities and atomic application of portfolio changes. An account-row lock provides a practical serialization boundary for this single-account scope. Event insertion, portfolio update, order-accounting checkpoint and audit/outbox entry belong in one transaction. Keep broker HTTP calls outside that transaction. Retrying a failed transaction does not imply it is safe to repeat a broker submission.[^11][^12]

The JSON backend remains useful for isolated simulation. Broker-backed release operation should require PostgreSQL after a reversible migration. Repair JSON deduplication and atomic writes as well, because simulation and regression results must be economically correct. Compare both stores against one accounting reducer, including reversals, duplicate fills, and fees.

### Halt, cancellation and rollback

Use explicit states: STARTING, RECONCILING, RUNNING, HALTING, HALTED and RECOVERY_REQUIRED. HALTING immediately rejects new exposure, submits permitted cancellations, and keeps reconciliation alive. HALTED means no owned open orders remain and late fills have been accounted for. Cancellation refusal, unknown broker state, missing credentials or timeouts produce RECOVERY_REQUIRED. The operator must never see a completed halt merely because the strategy loop exited.

An emergency halt does not automatically mean liquidation. Cancel-first, reduce-only exits and full liquidation are distinct policies. For the initial release, a stop-loss reduction must pass a dedicated authorization path that cannot increase absolute exposure or cross into a short. The owner defines whether automatic liquidation is permitted. External/manual orders are surfaced and trigger review; the platform does not silently cancel orders it does not own.

Rollback first proves a safe halt, then starts the previously accepted configuration in its separate paper/simulation namespace. Paper and live accounts must have separate ledgers, IDs, balances and secrets. Changing an environment value cannot convert live holdings into paper holdings. Report requested, acknowledged and observed states separately, including the remaining real exposure.

### Risk and market data

Create one versioned risk policy and one authoritative marked portfolio snapshot. Risk and Compliance should not independently calculate NAV with different valuation conventions. Include outstanding reservations when calculating projected concentration, cash and leverage. A proposal that reduces exposure must not be rejected solely because available cash is low; any cross-zero portion is a separate increase in risk.

The initial proposed policy follows repository intent: 10% NAV single-name cap, 25% sector cap when classification is available, gross leverage at most 1.0 for the initial mandate, 2% session-loss pause, 5% hard halt, and explicit liquidity/slippage limits. These are conservative design choices resolving conflicting existing documents; they require owner approval before use with capital. Missing sector or liquidity data blocks new exposure instead of inventing compliance.

ETFs retain the single-instrument cap and contribute to sector exposure through approved, dated look-through weights. The map must have source identity, availability date, checksum, weights summing to one and an owner-approved freshness limit. Missing mappings block new ETF exposure; do not manufacture a single-sector label for a broad fund. Combine fund look-through with direct-stock exposure in the same sector check.

Persist the session-opening equity, external cashflows, active halt, policy hash and session identifier. Use a venue calendar and marked valuations, not tick counts, for loss windows. Define cashflow-adjusted loss and rolling-session drawdown; enforce the agreed action rather than only logging a warning. The current default holiday-only calendar and fixed local scheduler hours require session-aware replacement. exchange_calendars supplies a useful boundary, but live execution must also check the broker clock.[^5]

Canonical market/research records need event time, availability time, receive time, source, revision and checksum. Preserve original timestamps through cache and fallback. Reject non-finite/non-positive prices, missing/future timestamps and excessive quote age at both decision and submission boundaries. A failed news feed may disable a news strategy while a price-only strategy continues under an explicitly approved input policy; it must not be silently replaced by invented sentiment.

Missing risk history is an unavailable estimate, not zero VaR. Warm-up should block increased exposure while permitting controlled reductions. LEAN explicitly distinguishes warm-up and trading readiness; this is a useful model for an observable Agenthedge warm-up state.[^13] VaR must define sampling frequency and horizon, align returns and address correlation. Historical daily estimates cannot be computed from an arbitrary number of intraday ticks without a defined conversion.

### Strategy evaluation and learning

Preserve the current strategy interface behind a canonical snapshot and injected clock. Replay must exercise the same Director, strategy, Risk, Compliance and Execution decision contracts as normal operation. Market snapshots and risk ticks must occur in a documented deterministic order. Economic fills should use the same ledger reducer in every mode, with the simulator supplying different execution events.

Completed daily data becomes usable only at its availability time. A strategy using the close cannot fill retroactively at that close. Define next-eligible-event order handling, price gaps, partial fills, order expiry, spread, fees and volume constraints. LEAN's equity fill model is a concrete reference for explicit timing and price rules; copying its headline performance is not a validation method.[^14]

Build an input manifest with point-in-time fundamentals/news, split/dividend handling and an explicit adjusted-price convention. Historical data that lacks availability/revision provenance cannot qualify a fundamental or news strategy. Start with price-only research when necessary and label disabled strategy families honestly. Avoid model-training leakage: current LLM knowledge is not automatically historically available information.

Adapt Freqtrade's lookahead checks into prefix and future-mutation tests: decisions through time T must remain unchanged when later observations are appended or altered. Also vary initialization history to detect unstable indicators. Freqtrade's commands operate on its own strategy contracts, so they cannot be run directly against Agenthedge. Its analysis-specific overrides must not be copied into production risk configuration.[^15][^16]

Freeze a candidate and compare gross/net returns, benchmark, drawdown, turnover, exposure and performance by regime. Record all tried configurations; keep a final holdout untouched. Recommended initial research minimum: three years of adequately sourced daily history and 100 closed trades per candidate, or an explicit insufficient-evidence outcome for low-turnover strategies. These are screening rules, not statistical proof or a promised edge. The owner chooses the objective and risk budget before seeing holdout results.

Learning should attribute economic outcomes to entry decisions and position lots. Log weight proposals separately from active weights; deploy a new strategy/weight hash only after acceptance. Automatic risk penalties may reduce or disable a strategy, but the initial live release should not autonomously increase its risk budget. This preserves the learning objective without allowing an unreviewed model update to invalidate the release dossier.

### Operator experience and deployment

Retain the current simulation launcher. Add an operator view attached to the durable runtime instead of starting another process-local broker runtime from each browser tab. It must display actual mode/account, freshness timestamps, cash/equity, positions, pending reservations, order/fill states, agent decisions, blocked reasons and lifecycle status. Stale or disconnected views must be visually unambiguous.

The supported workflows include simulation start/stop, paper preflight/start, halt with outstanding orders, restart recovery, session closeout and evidence export. Live activation remains a deliberate supervised operation with exact account and release identity. A refresh or double click cannot duplicate a command; CLI and UI consume the same idempotent command interface.

Choose one designated host and one broker-capable worker per account. Keep Windows developer parity and use the existing Linux CI/PostgreSQL path for release checks. Add restore drills, log retention, clock checks, alert delivery to a configured test receiver, and a process-termination/recovery scenario. A cloud migration or public authenticated dashboard is a separate deployment scope; no platform choice is assumed here.

## Release gates

| Gate | Required evidence | Failure result |
| --- | --- | --- |
| G0 Baseline and mandate | Exact SHA, regression matrix, supported instruments/account, policy and data manifests | Planning/fixture-only |
| G1 Economic correctness | Decimal fixtures, no duplicate/lost effects through crash points, broker-history reconciliation, reversible migration | Broker scheduler disabled |
| G2 Safety and input integrity | Gradual loss, aggregate reservations, stale-feed veto, cancel races, persistent halt and observed rollback | New exposure blocked |
| G3 Strategy acceptance | Runtime/replay parity, causal fills, bias tests, frozen holdout and approved objective | Research/paper-only candidate |
| G4 Operational paper acceptance | At least 5 complete clean sessions after safety fixes, real closeouts and zero unresolved reconciliation errors | No dependable-paper claim |
| G5 Live pilot acceptance | G1-G4; 20 representative paper sessions for this release policy, fault drills, account/credentials, approved caps, signed release identity | Live disabled |
| G6 First-release completion | Observed supervised pilot, operator workflows, restore/runbook and residual-risk acceptance | Pilot stays bounded or returns to paper |

The proposed G5 policy fixes the previous variable 1/3/5-session defaults. The 20-session count is operational evidence only. A clean day with no strategy trades can count toward availability but not toward strategy-performance evidence. Material accounting, risk, strategy or data-policy changes invalidate the applicable gate; evidence must be regenerated rather than backdated.

## Delivery estimate

The completion backlog is estimated at **45-65 engineer-days**, including integration, review, regression work and documentation, excluding waiting for account/data access and excluding discovering a profitable strategy. With two effective engineering contributors, plan **8-12 calendar weeks** for the first complete release including paper observation. With one contributor, plan **12-16 weeks**. A narrowly bounded live pilot can still occur earlier if its gates pass; it is not equivalent to completion of all product workflows.

| Window with two contributors | Work and evidence |
| --- | --- |
| Weeks 1-2 | Regression baseline, truthful control status, accounting and durable journal; parallel canonical data/risk policy |
| Weeks 3-4 | Broker recovery/cancellation, risk enforcement, calendar, operator commands; enter qualification paper sessions after G1/G2 |
| Weeks 4-6 | Causal replay, point-in-time inputs, strategy evaluation, operator UI and restore drills |
| Weeks 5-9 | Accumulate representative paper sessions; fix failures and restart affected evidence windows |
| Weeks 8-12 | Exact-release review, supervised pilot, closeout and first-release completion |

The earlier audit's 2-3-week paper repair and 6-10-week live-pilot ranges were narrower. This program adds durable accounting migration, corporate-action reconciliation, a complete operator workflow, reproducible strategy evaluation and recovery acceptance. Parallelism helps independent modules; it does not compress market-session observation or justify simultaneous edits to shared execution/persistence code.

## Full-program expansion

The original executive specification names equities, FX, crypto and derivatives. Keep its remaining scope visible:

- **Multi-asset release:** introduce instrument/venue identity, currency conversion, precision/lot rules, fee/settlement models, 24/7 calendars, funding and asset-specific exposure. Qualify one venue/asset at a time using G1-G6. NautilusTrader becomes a stronger engine candidate at this boundary.[^1]
- **Short/margin/derivatives release:** model borrow availability/cost, margin, assignment/exercise/expiry and liquidation rules before enabling those trades. Each instrument family needs its own risk policy and paper acceptance.
- **Advanced research/learning release:** add independently evaluated model families and controlled weight promotion, retaining immutable feature/model/data identities. Never substitute agent discussion for economic validation.
- **External-customer product:** account onboarding, authentication, tenant isolation, per-customer keys, billing, support and jurisdiction-specific legal assessment form a separate product program.

These expansions are not included in the 45-65-day first-release estimate. A defensible total calendar date requires selection of instruments, venues, account model and permitted autonomy. Each expansion begins with a bounded feasibility/requirements slice and ends with the same concrete operational gates; none is implicitly approved for execution by this design.

## Sources

All web sources below were accessed September 14, 2026; documentation pages are moving references unless an immutable commit is shown. Recommendations, proposed thresholds, module boundaries and effort estimates are analytical judgments. No repository popularity or unverified performance claim is used as readiness evidence.

[^1]: Nautech Systems. [NautilusTrader repository](https://github.com/nautechsystems/nautilus_trader/tree/5dec1b07c00a6af6dec3a943b4ce473dbb276457), September 14 snapshot; architecture, integration and license reference.
[^2]: Alpaca. [alpaca-py repository](https://github.com/alpacahq/alpaca-py/tree/48fd544334a595c53e386043cc9282824b1e9c58), September 10 commit; official SDK and license.
[^3]: QuantConnect. [LEAN repository](https://github.com/QuantConnect/Lean/tree/02e491cc4b2fb6b09a2a8c0b82b64243bbfa7d76), September 14 snapshot; engine architecture and license.
[^4]: Freqtrade contributors. [Freqtrade repository](https://github.com/freqtrade/freqtrade/tree/eec4eb074bd919d405fb60be8eaee49d3a49b511), September 14 snapshot; framework scope and license.
[^5]: exchange_calendars contributors. [XNYS implementation](https://github.com/gerrymanoim/exchange_calendars/blob/1eabe9da1f7b159dda12284e8a684f76f6323523/exchange_calendars/exchange_calendar_xnys.py); [repository](https://github.com/gerrymanoim/exchange_calendars/tree/1eabe9da1f7b159dda12284e8a684f76f6323523), September 14 snapshot.
[^6]: Hypothesis contributors. [Stateful tests](https://hypothesis.readthedocs.io/en/latest/stateful.html); [license](https://raw.githubusercontent.com/HypothesisWorks/hypothesis/master/LICENSE.txt), undated documentation/current license.
[^7]: Agenthedge. [September 14 readiness audit](../../../.cache/audits/2026-09-14/READINESS-AUDIT.md), including saved probe results and test logs; [executive specification](../../execspec.md), [risk policy](../../RISK_MANAGEMENT.md), [roadmap](../../ROADMAP.md). Local repository evidence, not an external certification.
[^8]: Alpaca. [Websocket Streaming](https://docs.alpaca.markets/us/docs/websocket-streaming), order/trade events and execution identifiers.
[^9]: Alpaca. [Account Activities](https://docs.alpaca.markets/us/docs/account-activities), historical economic activities.
[^10]: Alpaca. [Placing Orders](https://docs.alpaca.markets/us/docs/orders-at-alpaca), lifecycle and order-type semantics.
[^11]: PostgreSQL Global Development Group. [PostgreSQL 16 transaction isolation](https://www.postgresql.org/docs/16/transaction-iso.html), concurrency and retry semantics.
[^12]: PostgreSQL Global Development Group. [PostgreSQL 16 INSERT](https://www.postgresql.org/docs/16/sql-insert.html), unique-conflict handling.
[^13]: QuantConnect. [Warm Up Periods](https://www.quantconnect.com/docs/v2/writing-algorithms/historical-data/warm-up-periods), readiness before trading.
[^14]: QuantConnect. [Equity fill model](https://www.quantconnect.com/docs/v2/writing-algorithms/reality-modeling/trade-fills/supported-models/equity-model); [implementation](https://github.com/QuantConnect/Lean/blob/02e491cc4b2fb6b09a2a8c0b82b64243bbfa7d76/Common/Orders/Fills/EquityFillModel.cs).
[^15]: Freqtrade contributors. [Lookahead analysis](https://docs.freqtrade.io/en/latest/lookahead-analysis/), experimental method and limitations.
[^16]: Freqtrade contributors. [Recursive analysis](https://docs.freqtrade.io/en/latest/recursive-analysis/), initialization sensitivity.
