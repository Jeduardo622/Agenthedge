# Agenthedge Execution Completion Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Execute checkbox steps task by task after fresh classification.

**Goal:** Make every broker order and economic fill recoverable, correctly accounted for and safely stoppable.

**Architecture:** Existing BrokerAdapter plus PostgreSQL economic journal, common decimal reducer, recovery-first runtime and explicit halt/drain controller.

**Tech Stack:** Python, Decimal, PostgreSQL 16, pytest; optional separately qualified alpaca-py adapter.

**Spec:** [Research/design](../specs/2026-09-14-platform-completion-research.md); [master plan/contracts](2026-09-14-platform-completion.md).

## Global constraints

All master-plan constraints apply. No live orders in unit/fault tests. Account/mode/event identities are mandatory. Never disable approvals or broker identity guards. Reviewer: execution/persistence specialist; tester: recovery/concurrency coverage. E4 is separately classified schema work.

## E1 — Truthful switch and rollback outcomes

**Modify:** `src/cli/paper_live_enablement_switch.py`, `tests/cli/test_paper_live_enablement_switch.py`. **Depends:** B1. **Produces:** honest plan-only status until O1 supplies an actual controller.

- [ ] Add a regression proving a packet-only rollback cannot claim applied/environment/broker mutation:

```python
def test_packet_is_not_an_applied_rollback(tmp_path):
    from cli.paper_live_enablement_switch import build_rollback_packet
    result = build_rollback_packet(artifact_dir=tmp_path, reason="probe",
        apply=True, confirmation="ROLLBACK LIVE SWITCH")
    assert result["rollback_applied"] is False
    assert result["env_var_mutation"] is False
```

- [ ] Run `poetry run pytest tests/cli/test_paper_live_enablement_switch.py -k packet_is_not -v`; preserve the baseline failure.
- [ ] Make unavailable application paths return blocked/plan-only with false mutation flags. Preserve dry-run output, typed-confirmation validation and existing artifact provenance. Do not pretend mutation of the CLI process changes another runtime.
- [ ] Run the whole switch suite; add apply-success only after O1 can return observed controller state. Reviewer verifies reports do not overstate actions.
- [ ] Commit only these files as a focused false-success repair; preserve live configuration.

## E2 — Broker origin and lifecycle boundary

**Modify:** `src/portfolio/broker.py`, `tests/agents/test_broker_execution.py`. **Optional later modification:** `pyproject.toml`, `poetry.lock`. **Depends:** B1. **Consumes:** existing BrokerAdapter. **Produces:** exact endpoint checks and explicit unknown/pending lifecycle mappings.

- [ ] Add constructor tests for wrong scheme, userinfo, nonstandard port, hostile suffix/prefix, query/fragment and paper/live cross-wiring; use fake keys only.

```python
def test_paper_host_is_exact():
    import pytest
    from portfolio.broker import AlpacaPaperBrokerAdapter
    with pytest.raises(ValueError):
        AlpacaPaperBrokerAdapter(api_key_id="fake", api_secret_key="fake",
            base_url="https://paper-api.alpaca.markets.attacker.example")
```

- [ ] Run `poetry run pytest tests/agents/test_broker_execution.py -k paper_host -v` and verify failure on the vulnerable baseline.
- [ ] Parse the URL and accept only the exact HTTPS origin for the selected mode. Preserve existing compatibility by normalizing a trailing slash and optional exact `/v2` suffix; reject all other paths, query, fragment and userinfo. Preserve timeout uncertainty and add raw-status handling so unknown states do not become trusted terminal states. Never follow an untrusted redirect with broker credentials.
- [ ] Run the adapter suite with mocked transport. If adopting alpaca-py, use a separate commit/PR, pin a released version and replay every old contract test. Verify request/response mapping and cancellation readback; SDK installation is not the repair.
- [ ] Reviewer inspects identity/redaction and tester confirms no real HTTP path in unit tests; commit the bounded change.

Reference: [alpaca-py client](https://github.com/alpacahq/alpaca-py/blob/48fd544334a595c53e386043cc9282824b1e9c58/alpaca/trading/client.py). A cancellation method return does not establish terminal cancellation.

## E3 — Common economic reducer and corrected fills

**Create:** `src/portfolio/accounting.py`, `tests/portfolio/test_accounting.py`. **Modify:** `src/portfolio/store.py`, `src/portfolio/postgres_store.py`, `src/agents/impl/execution.py`, `tests/agents/test_broker_execution.py`. **Depends:** B1. **Produces:** AccountingState/PositionState/apply_trade in master catalogue.

- [ ] Add exact-decimal reducer cases for buy/add, partial sale, close, reversal, fees and fractional source quantities (even though first-release submissions are whole shares):

```python
def test_reversal_resets_remaining_basis():
    from decimal import Decimal as D
    from portfolio.accounting import AccountingState, apply_trade
    state = AccountingState(cash=D("1000"), realized_pnl=D("0"), positions={})
    state = apply_trade(state, symbol="SPY", quantity=D("1"), price=D("100"))
    state = apply_trade(state, symbol="SPY", quantity=D("-2"), price=D("120"))
    assert state.cash == D("1140")
    assert state.realized_pnl == D("20")
    assert state.positions["SPY"].quantity == D("-1")
    assert state.positions["SPY"].average_cost == D("120")
```

- [ ] Run `poetry run pytest tests/portfolio/test_accounting.py -v`; expected initial failure is absent module/function. Add the saved partial-fill probe as an actual agent regression with expected cash780/basis110.
- [ ] Implement the reducer. Separate closing quantity from any new opposite-side quantity; the new side takes execution price. For cumulative reports calculate `delta_value = q_new * avg_new - posted_value`, then `delta_price = delta_value / delta_q`. Reject non-finite values and route same-quantity corrections to reconciliation. Both stores call the reducer.
- [ ] Run portfolio/broker tests and dedicated PostgreSQL parity tests with identical event sequences. Specify broker rounding at serialization boundaries; do not round each intermediate calculation. Test aggregate versus incrementally received fills for identical final economics.
- [ ] Reviewer checks accounting independently against hand calculations; commit the reducer and both integrations together.

## E4 — Durable intents, journal and atomic portfolio application

**Create:** `src/portfolio/journal.py`, `tests/portfolio/test_journal.py`, `tests/integration/test_execution_journal.py`, `scripts/migrate_execution_journal.py`. **Modify:** `src/infra/postgres.py`, `src/portfolio/postgres_store.py`, `src/portfolio/store.py`, `src/agents/impl/execution.py`. **Depends:** E3. **Produces:** journal interfaces in master catalogue plus migration dry-run/readback report.

- [ ] Specify unique `(account_id, mode, event_id)` and `(account_id, mode, client_order_id)` identities, persisted submission uncertainty, posted cumulative value, reservation state and audit/outbox records. Write crash/concurrent-consumer tests before implementation. Required invariant:

```python
def assert_single_application(journal, event, read_cash):
    assert journal.apply_event(event) is True
    after = read_cash()
    assert journal.apply_event(event) is False
    assert read_cash() == after
```

The test fixture supplies a real dedicated PostgreSQL journal, one EconomicEvent and an account cash query; run the same event before/after connection loss and with two concurrent consumers.

- [ ] Run `poetry run pytest tests/integration/test_execution_journal.py -v` against the disposable database and capture missing-table/API failures. Unit tests must not quietly stand in for SQL transaction proof.
- [ ] Extend the existing account/fill transaction: lock account, insert dedup event, update positions/cash, advance economic checkpoint and append audit/outbox, then commit. Store intent before network send; do not hold DB locks across HTTP. Close accounting only when all observed economics are applied. JSON mode stores dedup state with portfolio atomically; recovery detects incomplete order/portfolio transitions. Broker mode requires PostgreSQL.
- [ ] Inject termination before send, after send, before/after commit and before acknowledgment. Verify migration dry-run counts, decimal conversion policy, backups and restore on synthetic copies. Old rows with insufficient provenance block automatic import. Add explicit transactional migrations rather than relying on CREATE TABLE IF NOT EXISTS to alter existing tables.
- [ ] Reviewer approves schema/rollback and tester verifies Windows/Linux parity. Commit schema and migration evidence; any real database application remains a separately scoped action.

References: [Nautilus reconciliation](https://github.com/nautechsystems/nautilus_trader/blob/5dec1b07c00a6af6dec3a943b4ce473dbb276457/crates/execution/src/reconciliation/mod.rs), [durable writer](https://github.com/nautechsystems/nautilus_trader/blob/5dec1b07c00a6af6dec3a943b4ce473dbb276457/crates/event_store/src/writer/mod.rs), [PostgreSQL isolation](https://www.postgresql.org/docs/16/transaction-iso.html). Use committed events as the checkpoint boundary; do not assume an atomic JSON rename coordinates two files.

## E5 — Recovery and economic reconciliation

**Create:** `src/portfolio/reconciliation.py`, `tests/portfolio/test_reconciliation.py`. **Modify:** `src/portfolio/broker.py`, `src/agents/impl/execution.py`, `src/agents/runtime.py`, runtime and broker tests. **Depends:** E2/E4. **Produces:** ReconciliationReport; new risk permitted only when complete and mismatch-free.

- [ ] Add fixtures for POST accepted then timeout, lookup initially missing, late fills, pagination gaps, recent closed order with unapplied economics, manual orders, fees, dividends, splits and corrections. Expected recovery ledger:

```text
intent durable -> POST outcome unknown -> restart -> lookup same client ID
=> exactly one observed broker order; no new client ID; one economic posting
incomplete activity page => complete=false; new submissions=0
```

- [ ] Run `poetry run pytest tests/portfolio/test_reconciliation.py tests/agents/test_runtime.py -k 'reconcil or recovery' -v`; verify new failures represent missing behavior.
- [ ] Reconcile before RUNNING, after stream reconnect and periodically while halted. Fetch complete activity/order windows, including recently closed orders, using durable cursors plus overlap/deduplication. Compare economic quantities and cash flows; normalize broker basis conventions rather than treating unrelated tax-basis differences as new fills. Unexplained differences block new risk and stay visible.
- [ ] Run disconnect/restart/duplicate histories and external cashflow/corporate-action fixtures. In a subsequently authorized paper session verify actual account/order identities, paging and readback. Unknown submissions stay unresolved until proven; never resubmit solely because a read timed out.
- [ ] Reviewer checks reconciliation completeness and incident behavior; commit the focused recovery change.

## E6 — Effective halt, cancellation and reduce-only stop loss

**Create:** `src/ops/control.py`, `tests/ops/test_control.py`. **Modify:** `src/agents/impl/execution.py`, `src/agents/runtime.py`, `src/agents/impl/risk.py`, runtime/broker tests. **Depends:** E5 and R1 policy. **Produces:** ControlResult; no completed halt claim without observed order drain.

- [ ] Write race scenarios and expected state transitions:

```text
RUNNING + halt(command A) => HALTING; all new-risk approvals rejected
partial fill during cancel => economic event posted; remaining cancel tracked
cancel acknowledged but order open => HALTING
deadline or broker unavailable => RECOVERY_REQUIRED
all owned orders terminal + reconciliation complete => HALTED
repeat command A => same result; no repeated economic effect
```

- [ ] Run `poetry run pytest tests/ops/test_control.py -v` and capture initial failures. Include a concurrent approval/kill boundary and restart while cancellation is pending.
- [ ] Persist the risk block first, cancel only owned orders, keep reconciliation running and expose unresolved identities. Route risk.stop_loss through explicit reduce-only authorization: no increase in absolute position, no cross-zero, no bypass of account identity/compliance restrictions. Automatic flattening follows an owner-approved policy; halt alone does not imply flatten.
- [ ] Run control/runtime/broker suites and a synthetic failure drill with cancellation rejection and late fills. After local proof, observe a bounded paper cancel/fill drill with cleanup and account readback.
- [ ] Reviewer confirms independent safety behavior and no blind cancel-all; commit E6. O1 may now use this controller for truthful live/paper control.
