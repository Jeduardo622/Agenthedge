# Supervised live pilot

## Status and authority

This runbook describes a small, owner-supervised live pilot. It grants no live authority
and records no current qualification. Live start remains blocked until the owner supplies
and accepts the exact live account, capital caps, allowed universe, credential scope,
position-retention policy and rollback policy, and until twenty representative paper
sessions and every G0-G5 artifact pass for the exact release.

The first release is limited to one owner account, US-listed equities and ETFs, USD,
whole-share long new-risk limit orders, regular exchange hours and an explicit allowlist.
Shorts, margin, options, futures, FX, crypto, additional venues/currencies, customer
accounts and autonomous upward promotion are outside this pilot.

## Entry decision

Before creating a live start command, require all of the following:

1. Twenty complete, clean, observed paper sessions under one accepted source account and
   matching release identity. Sessions cover representative normal, no-trade and permitted
   recovery conditions; no-trade days do not count as trade samples.
2. Signed `live_start` evidence passing G0-G5: baseline and manifests; accounting,
   crash/replay, reconciliation and migration proof; session loss, aggregate reservations,
   stale-feed veto, cancellation races, persistent halt, observed rollback and current
   preflight; causal strategy acceptance; dependable paper operations; and representative
   sessions, fault drills, account readiness, approved caps and owner authorization.
3. Independent binding of the expected live `ReleaseIdentity`, trusted issuer keyring and
   paper qualification account. The live and paper accounts/modes remain separate durable
   namespaces. A paper-to-live environment change is not proof of live account identity.
4. Current redacted broker/provider health, exact live account readback, no unknown external
   orders, expected starting positions/cash, current venue clock, complete reconciliation,
   tested alerts and a staffed operator able to halt and reconcile.
5. An owner-signed pilot envelope outside Git: exact symbols, maximum position and capital,
   per-order and aggregate caps, maximum duration, stop conditions, retain/flatten decision,
   paper rollback target, reviewers and authorized operator.

Any mismatch, expired/future evidence, missing observation, open uncertainty or cap change
is a hold. Changes to code/configuration/policy/strategy/data identity require newly signed
evidence and any affected observations must be repeated.

The historical paper readiness/report/switch CLIs can summarize existing artifacts, but
their command-line entry points do not independently load the canonical release trust.
They cannot authorize the pilot. The durable worker's signed release decision and command
readback govern activation.

## Prepare separate workers

Start from a clean reviewed checkout at the approved SHA. Provision the live namespace and
the separately qualified paper rollback namespace in existing schema-v6/control-v1 stores.
Use distinct worker instances and state paths. Supply secrets only through reviewed process
environment variables; never put a DSN, issuer key or broker credential in a command,
artifact or Git.

The live worker must use `EXECUTION_MODE=live` and the exact approved account. Configure
the paired paper options together so rollback can address the separately running paper
worker:

```powershell
poetry run python -m cli.runtime worker-run `
  --trust-file C:\approved\live-trust.json `
  --evidence-file C:\approved\live-evidence.json `
  --checkout C:\installed\Agenthedge `
  --strategy-file C:\approved\strategy.json `
  --data-file C:\approved\runtime-data.json `
  --performance-file C:\agenthedge-state\live-performance.json `
  --audit-file C:\agenthedge-state\live-audit.jsonl `
  --report-directory C:\agenthedge-state\live-reports `
  --instance-id LIVE_WORKER_INSTANCE `
  --paper-account PAPER_ACCOUNT `
  --paper-release RELEASE_SHA `
  --paper-dsn-environment PAPER_POSTGRES_DSN `
  --max-iterations 1 --poll-seconds 1
```

Pairing does not start or qualify the paper worker. Keep that paper worker independently
fenced and capable of producing a fresh, exact linked start observation if rollback is
requested. Construction remains recovery-only and may reconcile uncertain state even when
release evidence has expired; it must not tick or submit a new order until the live gate
passes.

The one-iteration example exits and stops its Runtime. Before the pre-open coverage
window, wait for that process's lease to expire and launch the reviewed command
with a new instance ID and an explicitly approved positive `--max-iterations`
budget covering supervision and closing readback at the selected poll interval.
Keep this process and the separate paper target running while submitting commands
from another terminal. Repeated one-iteration launches cannot preserve an active
session or automatically resume a prior live start.

## Start and supervise the pilot

Run the same pre-open reconciliation and observed session-coverage procedure as paper,
against the live namespace. Confirm the signed dossier remains current immediately before
activation. Submit one stable live-start command:

```powershell
poetry run python -m cli.runtime control-submit `
  --account-id LIVE_ACCOUNT --mode live `
  --command-id PILOT_START_ID --action request_live_start `
  --release RELEASE_SHA --actor OPERATOR_ALIAS

poetry run python -m cli.runtime control-status `
  --account-id LIVE_ACCOUNT --mode live `
  --command-id PILOT_START_ID
```

Require `state=succeeded`, `applied=true`, exact live account/mode/release and a fresh
running observation before permitting the bounded pilot. Pending or acknowledged is not
applied. Keep the operator present and the worker lease, release evidence, market inputs,
session-risk state, reconciliation, owned-order lifecycle, alerts and broker clock visible.
Do not raise caps, add symbols, extend duration or restart under a different identity to
work around a block.

Follow the [evidence renewal procedure](worker-authority.md#renew-evidence-during-a-running-session)
for the live worker and separately for its paper rollback target. The current-preflight
artifact must remain within300 seconds; each replacement needs actual fresh observations,
the same independent trust/full identity and a newer signed issuance. A malformed or
invalid replacement stops new ticks. Correcting it cannot restart a halted worker or
restore a lost lease, and it does not relax the pilot's staffed supervision requirement.

When a stop condition occurs, issue a durable halt and retain its stable command ID:

```powershell
poetry run python -m cli.runtime control-submit `
  --account-id LIVE_ACCOUNT --mode live `
  --command-id PILOT_HALT_ID --action halt `
  --release RELEASE_SHA --actor OPERATOR_ALIAS
```

A halt blocks new risk and reconciles/cancels only owned orders. It is not an implicit
liquidation. Treat `HALTING`, cancellation rejection, unknown status, a missed deadline or
late fill as recovery required until durable reconciliation proves the result.

If the approved response is rollback to paper, keep the live account halted and submit:

```powershell
poetry run python -m cli.runtime control-submit `
  --account-id LIVE_ACCOUNT --mode live `
  --command-id PILOT_ROLLBACK_ID --action rollback_to_paper `
  --release RELEASE_SHA --actor OPERATOR_ALIAS
```

Rollback succeeds only after the exact linked command has a fresh running observation from
the separately configured paper worker. An unrelated or historical paper start cannot
satisfy it. Rollback does not move, flatten or discard live positions; reconcile and retain
them under the approved live close policy.

## Closeout and decision

At or after the actual XNYS close, record the closing observation and submit a stable
`close_session` command for the live namespace. Require a source-backed `CLOSED` readback,
same-revision complete reconciliation, no unresolved owned order, exact position/cash
readback and retained audit/alert evidence. Do not backdate missing opening or closing
coverage.

G6 is evaluated after the pilot and is never required to start it. The signed `closeout`
decision requires G0-G6 and retains the twenty qualifying paper sessions plus observed
pilot, operator-workflow, restore-runbook and residual-risk-acceptance artifacts. Reviewer,
tester and owner record an explicit accept/hold outcome against the exact identity. A
failed pilot remains in the evidence set with its diagnosis; it is not silently excluded.

Until twenty representative paper sessions, actual owner inputs, live credentials,
explicit authorization and a supervised observed pilot exist, the truthful result is
**hold: live pilot qualification incomplete**. Completion of this initial release also
leaves the excluded asset, venue, account and autonomy scope unqualified for later work.
