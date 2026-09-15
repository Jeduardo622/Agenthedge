# Agenthedge Operations and Release Completion Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans. Execute checkbox steps after fresh classification.

**Goal:** Make the platform operable through truthful commands, a durable dashboard, recovery drills and exact-release qualification.

**Architecture:** One broker-capable worker per account consumes durable idempotent commands. UI/CLI read the same state; reports summarize observed outcomes. Release acceptance binds code, configuration, strategy and data identities.

**Tech Stack:** Existing Python/PostgreSQL/Streamlit, pytest, CLI, Linux CI and supported Windows host.

**Spec:** [Research/design](../specs/2026-09-14-platform-completion-research.md); [master contracts](2026-09-14-platform-completion.md).

## Global constraints

All master constraints apply. No public UI exposure or remote authentication is added implicitly. Keep current simulation launcher safe. Reviewer: runtime/ops; tester: real workflow and recovery; ui-hardener: O2. Live actions require actual target/account/caps and acceptance at execution time. A report is never evidence that its claimed operation happened.

## O1 — Durable control commands and real rollback

**Create:** `src/ops/commands.py`, `src/ops/worker.py`, `tests/ops/test_commands.py`, `tests/integration/test_control_commands.py`. **Modify:** `src/cli/runtime.py`, `src/cli/paper_live_enablement_switch.py`, `src/agents/runtime.py`, `src/infra/postgres.py`. **Depends:** E1/E4/E6/R1/R5. **Produces:** command API in master plus `status(command_id: str) -> dict[str, object]` with requested_at, acknowledged_at, observed_at, state, release/account/mode identity.

- [ ] Write duplicate-command, stale-release, wrong-account and partial-failure cases:

```text
same command ID submitted twice => one durable command and one transition
expected release mismatch => rejected before broker request
rollback requested, worker disconnected => pending/recovery, applied=false
halt acknowledged, open order remains => applied=false, HALTING
paper worker observed after live halt => rollback control complete;
live positions remain explicitly reported, never moved into paper ledger
```

- [ ] Run `poetry run pytest tests/ops/test_commands.py tests/integration/test_control_commands.py -v`; use a dedicated PostgreSQL fixture and fake broker/controller.
- [ ] Implement allowed actions `start_paper`, `halt`, `reconcile`, `close_session`, `request_live_start`, `rollback_to_paper`. Persist command and authorization context. Worker checks account/mode/release, fencing token and gate status before action. Halt/drain via E6; rollback keeps live exposure/accounting visible and starts separate paper state only after acceptance. Unknown worker state is not success.
- [ ] Exercise command delivery/restart and failure at each controller step. Verify no process-local os.environ mutation is treated as configuration delivery. Concurrent workers must not both submit orders; retain a single-owner deployment contract and test lease loss around submission. Never claim database fencing can revoke an HTTP request already in flight.
- [ ] Reviewer confirms live paths remain unavailable without evidence and owner authorization; commit control protocol and CLI integration.

## O2 — Complete operator workflows

**Modify:** `src/observability/dashboard.py`, `src/observability/dashboard_helpers.py`, `src/observability/state.py`, `src/cli/dashboard.py`. **Create:** `src/observability/operator_view.py`, `tests/observability/test_operator_view.py`, `docs/OPERATOR_GUIDE.md`. **Depends:** O1. **Consumes:** status/submit, canonical portfolio/risk/reconciliation state. **Produces:** one operator view of the durable worker; simulation controls remain isolated.

- [ ] Add rendering and interaction acceptance fixtures:

```text
disconnected worker => show stale timestamp and unavailable controls
double-click start with same command ID => no second command
wrong account/release => visible reason, no submission
HALTING + pending order => show order and progress, never 'stopped'
empty/no-trade session => show no trades, not missing/zeroed balance
browser refresh => current durable state, no new runtime
```

- [ ] Run `poetry run pytest tests/observability/test_operator_view.py tests/observability/test_dashboard_helpers.py -v`; use Streamlit test support where available, with pure presenters for deterministic assertions.
- [ ] Display actual mode/account alias, release, freshness, balances/equity, positions, reservations, order/fill status, strategy decisions/non-participation, active risk policy and halt reasons. Add preflight, paper start, halt, reconcile, closeout and export flows through O1. Live activation shows exact reviewed identity and deliberate confirmation; a simulation button never switches modes.
- [ ] Run route/workflow browser QA: start simulation, refresh, stop, inspect paper preflight, create a fake order lifecycle, halt through late fill, restart recovery and export. Test keyboard navigation, empty/error states and narrow viewport. Save sanitized screenshots plus observed command IDs; do not label rendering tests broker proof.
- [ ] ui-hardener reviews usability, reviewer checks control authority, tester checks duplicate operations; commit operator view and guide.

## O3 — One release evidence policy

**Create:** `src/ops/release_gate.py`, `config/promotion-gates/platform_release.json`, `tests/ops/test_release_gate.py`, `docs/release/evidence-schema.md`. **Modify:** `src/agents/config.py`, `src/cli/paper_review_board.py`, `src/cli/paper_live_readiness_report.py`, `src/cli/paper_live_enablement_switch.py`, related CLI tests. **Depends:** E5/R1 for the gate core; integrate O1 and S4 afterward without a circular startup dependency. **New interface:** `evaluate_release(evidence: dict[str, object], *, stage: str, expected: ReleaseIdentity, now: datetime) -> tuple[bool, tuple[str, ...]]`. Frozen `ReleaseIdentity` fields: sha, account_id, mode, config_hash, policy_hash, strategy_hash, data_hash (all str), supplied by the trusted controller/configuration independently of candidate evidence. Allowed stages: paper_start (G0-G2), dependable_paper (G0-G2 plus G4), live_start (G0-G5), closeout (G0-G6). Reject unknown stage; post-pilot G6 must never be required to start the pilot.

- [ ] Add fail-closed tests for mismatched SHA/policy/strategy/data/account, expired/future timestamps, truncated artifacts, missing session closeouts and a boolean-only readiness assertion:

```python
def test_boolean_assertion_is_not_evidence():
    from datetime import datetime, timezone
    from ops.release_gate import ReleaseIdentity, evaluate_release
    identity = ReleaseIdentity("9426494ffa368f43f7d8c4cd6aee11588e5e0385",
        "synthetic-account", "live", *(["0" * 64] * 4))
    ok, reasons = evaluate_release({"three_session_stability_confirmed": True},
        stage="live_start",
        expected=identity,
        now=datetime(2026, 9, 14, 20, tzinfo=timezone.utc))
    assert ok is False
    assert reasons
```

- [ ] Run `poetry run pytest tests/ops/test_release_gate.py -v` plus the current readiness/switch suites; preserve tests covering redaction and artifact provenance.
- [ ] Implement the stage-specific G0-G6 requirements in one policy. Proposed thresholds: 5 complete clean sessions for operational paper acceptance, 20 representative sessions for live pilot, no unresolved economic mismatches, current preflight and exact identity. Research paper sessions can start after G0-G2 without first proving strategy acceptance; live start requires G0-G5, while pilot observations are checked only at closeout. Record evidence signatures/hashes from the accepted issuer; fail on missing identity. Replace bare environment assertions as sufficient approval while preserving compatible parsing with explicit deprecation errors.
- [ ] Test one evidence set across every CLI/runtime consumer for identical outcome. Late/malformed evidence cannot override a blocker. A no-trade session may count availability but not trade sample size. Changes to economic/risk/strategy/data contracts invalidate affected evidence and require rerun.
- [ ] Reviewer checks that policy is singular and no alternate live entry path bypasses it; commit gate and consumers.

## O4 — Deployment, restore, observability and fault drills

**Create:** `scripts/qualify_runtime.py`, `tests/ops/test_qualification.py`, `docs/release/recovery-drills.md`. **Modify:** `docs/OPS_RUNBOOK.md`, `docs/OBSERVABILITY.md`, `docs/SECURITY.md`; modify existing CI workflows only when needed for concrete tests. **Depends:** E4-E6/O1/R5. **Produces:** sanitized machine-readable drill report with run ID, code/config hashes, observations and unresolved failures.

- [ ] Write a deterministic drill driver using fake transport and temporary storage; assert failures cannot be reported as passes:

```text
kill process after broker accepts before acknowledgment => one order recovered
database unavailable => zero new submissions, visible recovery state
stream disconnect => REST reconciliation before resume
cancel rejected => no false HALTED
restore backup plus replay events => identical cash/positions/checkpoint
second worker starts => no concurrent broker submissions
```

- [ ] Run `poetry run pytest tests/ops/test_qualification.py -v`; inject failures and require the expected nonzero qualification outcome.
- [ ] Implement supervised worker startup/restart, bounded recovery, backup/restore and log/audit retention for the selected host. Report clock skew, provider age, order/reconciliation lag, unresolved submissions and control state. Use a local fake alert receiver first; real notifications require explicit destination authorization.
- [ ] Run Windows-local and Linux-container drills; execute actual process termination and PostgreSQL restore on disposable resources. Preserve coverage, signing and dependency audit. Measure recovery/detection latency and compare with the accepted policy rather than inventing a service-level claim.
- [ ] Reviewer assesses deployment blast radius and tester checks restore equivalence; commit driver/runbook. Hosted deployment, secrets and real database changes remain separately classified actions.

## O5 — Broker-paper qualification and supervised live pilot

**Create:** `docs/release/paper-qualification.md`, `docs/release/live-pilot.md`. **Modify:** existing paper session/report CLIs only for observed missing evidence, with separate classification. **Depends:** G1/G2, then G3/O2/O3/O4 for live. **Produces:** real dated session dossiers and an explicit go/hold decision.

- [ ] Before any broker action, identify actual account/mode, ownership prefix, credentials via redacted health, allowed universe, caps and cleanup/retain-position policy. Verify no unknown external orders. A blocked preflight is recorded without trying to switch to another account or widening scope.
- [ ] Run a bounded paper lifecycle: submit within approved caps, observe partial/full/cancel states, reconcile economic activity, close the session and read back residual orders/positions. Do not fabricate partial fills if the venue does not produce them; combine real normal lifecycle proof with labeled synthetic rare-event tests.
- [ ] Accumulate 5 clean sessions after G1/G2 for dependable paper, and 20 representative sessions for the proposed live gate. Include restart/reconnect/cancel/recovery drills where safe. Record no-trade days honestly. Material repairs reset affected observation evidence; failures do not become excluded days without explanation.
- [ ] Evaluate exact-release G0-G5 and strategy results. If accepted by the owner with actual live caps/account, conduct a small supervised pilot, keep control and reconciliation running, and observe halt/closeout. Do not assume a paper-to-live environment change proves account state or cleanup.
- [ ] Reviewer and tester sign off actual artifacts and residual risks. Commit only sanitized documentation/evidence references; leave capital/credentials out of Git. Continue to I1 only after the required outcomes exist.

## Expansion entry rules

The first-release closeout must list the original multi-asset/advanced-autonomy goals as remaining scope. Each added venue or asset family needs a new instrument/account model specification, provider qualification, economic fixtures, risk policy, paper evidence and exact-release review. A customer-facing product requires its own authentication/tenant/security design. Do not start these automatically after O5.
