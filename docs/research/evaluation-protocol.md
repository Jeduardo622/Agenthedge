# Strategy evaluation protocol (S4a/S4b)

This harness produces research evidence. `screen_passed_research_only` means the proposed sample and causality checks passed. It does not mean profitability, statistical significance, owner acceptance, or permission to trade. The profile in `config/promotion-gates/strategy_qualification.json` records proposed defaults. The qualification CLI uses these fixed sample defaults; it does not allow a candidate to lower them or assert owner approval.

## Freeze before execution

Construct immutable `Candidate` objects with exact strategy artifact SHA-256 and canonical configurations. Construct three `Partition` objects with aware UTC start/end, chronological records, stable record IDs, and explicit `available_at`. Intervals are half-open and ordered train → validation → holdout. Records are copied into immutable canonical JSON, so changing caller dictionaries cannot change evaluation data. Later revisions remain separate records; the supplied point-in-time adapter retains responsibility for availability/revision selection inside each replay.

`EvaluationProtocol` binds objective, all candidate configurations/code hashes, partition contents/bounds, thresholds, and harness version into a protocol hash. Supported objectives are net return and net return minus benchmark return. The trusted execution adapter independently supplies `strategy_hashes`; those must match the candidate registry exactly. The real adapter must compute hashes from actual installed strategy artifacts. Matching candidate text alone is not an installed-code attestation.

Create `ValidationHarness(protocol, execute, audit_path=NEW_PATH, strategy_hashes=TRUSTED_MAPPING)`. The new audit path must not already exist. Before invoking any callback, the harness flushes an append-only `protocol_frozen` record with objective, candidates, data/partition hashes, thresholds and trusted strategy mapping. Each attempt has start/result/failure records and a hash chain. Preserve the original audit and trusted protocol hash; this is a single-writer evaluation session, not an adversarial storage ledger or restartable coordinator.

## Execution callback

The callback accepts immutable `RunRequest` metadata and returns `RunResult`. `request.records` returns fresh copies of the frozen inputs. Normal validation receives train+validation data, never holdout; each callback must start a fresh isolated engine and use only records available at each simulated decision. Training is permitted only from the supplied training/warmup range. Do not reuse portfolio, learned weights or other mutable engine state across runs.

Return chronological decision rows with aware `timestamp`, including explicit no-action/risk-unavailable decisions. `prefix_equal` compares the entire row content through an inclusive cutoff, normalizes time zones and JSON key order, and ignores only top-level `run_id`. IDs, risk values and reasons are otherwise significant. A later engine adapter must normalize its nondeterministic operational identities consistently before returning rows; this harness does not silently discard risk provenance.

Return at least two strictly chronological `EquityPoint` values covering the exact requested evaluation bounds, plus an actual closed-trade count. Gross/net/benchmark curves must use the same capital basis and valuation times; external cash flows must be removed or supplied as consistent flow-adjusted indices by the adapter. Positive finite curve values are required. Turnover notionals are interval traded notionals, exposure is a consistently defined nonnegative fraction, and each point carries an explicit regime label.

Metrics are computed from these samples: gross/net/benchmark compounded returns, net excess return, sampled net peak-to-trough drawdown, traded notional divided by mean net value, mean exposure, regime observation counts and compounded interval net returns assigned to the ending point's regime. Sparse curves cannot establish intraperiod drawdown. Observation and trade counts remain visible. The callback is also rerun at twice its execution costs; this must alter fees/slippage, not prices or strategy parameters. Cost sensitivity is recorded separately from base results.

## Executable bias diagnostics

For each candidate, diagnostics use only that phase's available data. They run a prefix ending at the phase midpoint and a full-length replay, comparing decisions through that cutoff. A third full-length replay perturbs future numeric values/prices/volumes by 100 and future headline/news/fundamental payloads. Identity, source metadata and availability times stay fixed. Two additional prefix runs compare warmup beginning at the training start versus the later of the halfway pre-evaluation point and 30 days before evaluation.

Diagnostic callbacks deliberately receive full future rows in the full-length runs. This is necessary to detect a future-reading implementation; filtering all diagnostic inputs would hide the bug. Normal adapters must still enforce per-decision causality. The default generic perturbation supports the documented value/price/news fields; the later qualified dataset adapter must preserve its input schema and rebuild any content-derived transport checksums when adapting these diagnostic requests. Do not treat loader rejection of an invalid mutation as proof of lookahead bias.

Empty decision evidence, an unchanged perturbation input, unequal prefixes, future perturbation differences or warmup differences block selection. Exceptions are audited as failed attempts. Tests include leaking fixtures that read the last future value, horizon-wide future data, future news, and warmup-dependent state; these are detectors, not profitable strategies.

## Selection and untouched holdout

Evaluate every registered candidate once. Each validation report is copied before storage, so mutating the returned report cannot change selection. Selection chooses the highest frozen objective among candidates passing the screen, with lexical candidate name as a deterministic tie-break. Failures/insufficient candidates are retained in the audit. No new candidates or repeat evaluation can be added after selection.

The selected candidate and its hash are recorded before holdout access. `evaluate_holdout(data_hash=protocol.data_hash)` requires that exact unchanged data hash and permits one attempt only, including failures. It uses the same frozen candidate, objective, execution callback and predeclared diagnostic/cost runs. Changing objective or partition contents produces a different protocol and cannot continue the existing session. A new research protocol must be a new audit; it is not an untouched-holdout result for the old experiment.

## Proposed evidence minimums

Require at least three calendar years between actual earliest/latest available records, at least 252 distinct daily observation dates per required year, and at least 100 closed trades in the evaluated phase. The daily-date count is a coverage screen, not proof of complete exchange-session coverage or licensed/accurate data; the qualified data adapter supplies those checks. Insufficient samples remain `insufficient_evidence`. A failed bias check is `rejected` even when other samples are sufficient.

Lower thresholds require the exact entire protocol hash in an independently reviewed, caller-owned `reviewed_protocol_hashes` allowlist. The candidate cannot supply an approval boolean or change that allowlist. Exceptions remain research-only and never imply owner acceptance. Objectives and live risk budgets need separate owner approval before any promotion.

## Qualified engine adapter and CLI

`QualifiedValidationAdapter` constructs a fresh actual `BacktestEngine` for every callback, with the reviewed qualified dataset loader, sourced risk service, causal broker, economic journal and isolated performance tracker. Each candidate runs one installed family: momentum, value, macro, and catalyst when explicitly enabled. Missing news/fundamentals/research packets stay visible in the actual Quant no-proposal audit. Missing qualified risk inputs fail evidence. The adapter does not substitute an allow-all risk evaluator or alter strategy logic.

Installed Python source bytes are hashed independently of candidate text. Strategy instance parameters, decision environment, initial capital, symbols and any research packet are frozen into candidate configurations. Source/configuration drift before or during a replay rejects evidence. This is a local installed-source check, not a signed build or dependency attestation. Run the verifier with the same installed source revision.

The bundle and all original rows are copied before evaluation. Diagnostic future changes receive rebuilt record and bundle checksums, and each derived input is saved beside its engine artifacts. Raw OHLC remains executable/valuation/benchmark data; `reference_close` remains the separate adjusted decision series. Warmup truncation drops historical observations, while retaining earlier *causally available* universe/classification/liquidity/ETF metadata from the frozen bundle. It never restores future prices, news or filings. The actual risk estimator can still reject a shortened price history: for example, a 30-day warmup is insufficient for a 60-session estimator. Such a convergence failure is reported, not masked.

Economic evidence comes from saved engine daily NAV, trade/cash/split events and per-fill costs. Closed trades count flat or reversal inventory cycles; partial exits and open positions do not count as completed trades. Turnover uses actual trade quantity times execution price. Exposure uses the journal's public Decimal projection and raw marks. Gross NAV adds accumulated actual modeled spread/commission costs to net NAV, matching the engine's cost-addback convention; it is not a separately simulated frictionless portfolio. Twice-cost runs actually change execution costs.

The benchmark buys the first alphabetically ordered candidate symbol with the same initial capital. It uses raw marks, exact split share adjustments, and USD dividends on shares held at the explicit entitlement time; cash dividends are retained without reinvestment. Unsupported actions fail evidence. Regime labels describe positive/negative/flat benchmark interval returns, not inferred economic regimes. Bounds between sessions carry the last observed NAV; a half-open partition excludes a price close exactly at its end, while an inclusive diagnostic containing that close evaluates it. Interior missing held/working marks still fail in the engine.

Daily coverage counts the intersection of actual sourced XNYS price sessions across all candidate symbols. News, filings and duplicate revisions cannot inflate it. The minimum remains three years/756 sourced sessions and 100 closed cycles. Available synthetic short-window fixtures finish as insufficient evidence or rejected diagnostics, not as qualified profitable strategies.

Decision evidence retains actual strategy and risk outcomes. Operational references receive consistent time-scoped identities. Identity-dependent risk fingerprints are recomputed over complete normalized frozen candidate/state/reservation/market inputs; policy and source hashes are retained. Original observations, original risk artifacts and hashes remain in the saved observation artifact. A different risk rejection or economic input remains a prefix difference.

Example frozen protocol JSON (choose boundaries appropriate to an authorized qualified bundle):

```json
{
  "objective": "net_excess_return",
  "train": ["2018-01-02T21:00:00+00:00", "2021-01-04T00:00:00+00:00"],
  "validation": ["2021-01-04T00:00:00+00:00", "2024-01-02T00:00:00+00:00"],
  "holdout": ["2024-01-02T00:00:00+00:00", "2026-01-02T00:00:00+00:00"]
}
```

```text
poetry run python -m cli.backtest --symbol SPY --start 2018-01-02 --end 2026-01-02 --dataset-bundle licensed-bundle.json --validation-protocol frozen-protocol.json --storage-dir NEW_RESEARCH_DIR
poetry run python -m cli.promotion_gate --qualification-artifact NEW_RESEARCH_DIR/qualification.json --qualification-sha256 INDEPENDENTLY_PINNED_ARTIFACT_SHA256
```

The first command evaluates all enabled families and preserves every attempt. It returns success for a completed `insufficient_evidence` research outcome, failure for rejected/failed evidence. Holdout is not opened unless all family screens pass and selection is frozen. The second command verifies the caller-pinned artifact bytes, local engine/input/observation artifacts, audit hash chain, candidate/phase identities and statuses. It requires a passing selected holdout. Its best result is `QUALIFICATION_SCREEN_PASS_RESEARCH_ONLY owner_approved=false`; it cannot grant runtime/owner acceptance. Legacy catalyst promotion reports remain a separate interface.

Long-window licensed data, independently reviewed owner objectives and any actual passing holdout remain required evidence. No strategy profitability, live readiness or owner approval is claimed by these synthetic tests.
