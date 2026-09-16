# Durable control command storage (O1a)

This slice persists requests and observed outcomes. It does not start, halt, cancel,
reconcile, switch or authorize a broker worker. O1's controller consumer remains
required. An action named in a queued request is not evidence it happened.

## Explicit setup and identity

`migrate_control_commands(dsn, apply=False)` inspects the dedicated control schema.
Explicit `apply=True` creates control version 1 only after reviewed execution
journal version 6 exists. Store operations never create schema. The migration is
transactional and does not copy balances or change execution accounts. Preserve
control records during rollback; no destructive rollback API is provided.

Construct `CommandStore(dsn, account_id=..., mode=...)` for one paper/live namespace.
`submit` requires a canonical command ID, matching namespace, allowed action and
exact expected release commit. Authorization context is immutable request metadata,
not approval: pass operator aliases and evidence references, never signing keys.
The controller must independently load and validate signed release evidence and
owner authority before executing an action.

Same command identity and identical payload is idempotent. Different payloads
conflict. Actions are start_paper, halt, reconcile, close_session,
request_live_start and rollback_to_paper; mode-specific actions are checked.

## Worker protocol

Every process must generate a new unpredictable worker ID at startup and use one
sequential consumer. `acquire_worker` gives one active lease per account/mode,
bounded to five minutes, with a monotonically increasing fence token after expiry.
An existing active owner may renew only its identical release. Worker IDs must not
be reused across processes. The lease check reads database time after obtaining
the row lock, so a lock wait cannot make an expired lease appear current.

`claim_next` rejects stale-release requests before acknowledgment and permits one
acknowledged action per namespace. Acknowledgment is committed before consumer
I/O. `require_worker` is required immediately before each controller operation;
final broker submission also needs this consumer fence. Database fencing cannot
revoke an HTTP request already in flight. Single-owner deployment remains required.

Within that worker process, the installed execution journal and halt controller
share one gate per account/mode. It orders final submission against the durable
halt claim without holding a database transaction over HTTP. A halt request
invalidates pending ordinary dispatch authority before waiting for an entered
request; entered requests still require owned cancellation and reconciliation.
The wait consumes the existing halt deadline. A failed or timed-out halt claim
keeps that journal instance inhibited: inspect durable recovery state and use a
fresh verified worker before resuming. Successful ordinary close and authorized
rearm retain their existing behavior. This gate does not coordinate separate
worker processes or make an earlier timeout audit event an atomic submission stop.

If a lease expires after acknowledgment, a new owner marks the command
recovery_required. It must inspect actual controller state; the store never
automatically retries that action. Recovery adoption/readback is a subsequent
consumer interface, not implemented by this storage slice.

## Truthful status

`status(command_id)` reads only the bound namespace and includes request,
acknowledgment and observation times. Pending, rejected and recovery_required
statuses always return applied=false. `record_observation` requires the current
worker and claimed command. Success additionally requires matching controller
account/mode/release, the action's completed state, no unresolved findings, and
readback no older than 30 seconds. Halt, close and rollback require no open owned
orders. Freshness and worker lease are checked again in the final update.

The success timestamp is the supplied controller observation time. A prior success
is historical evidence, not a claim that the worker remains healthy now. The later
operator view must display its age and fresh controller/worker state separately.

## Verification and remaining work

Actual isolated PostgreSQL tests cover duplicate requests, account separation,
competing owners, stale release rejection, uncertain restart, stale owners,
partial halt, stale/mismatched controller readback and expiry during a row-lock
wait. No real broker request is performed. Actual E6 controller use, release-gated
start, rollback readback, recovery adoption, final submission fencing, durable
operator UI and browser workflows remain O1b/O2.

## CLI request and readback

With an explicitly supplied `POSTGRES_DSN` and the schemas provisioned above, use
`python -m cli.runtime control-submit --account-id ACCOUNT --mode paper_broker
--command-id REQUEST --action halt --release COMMIT_SHA --actor OPERATOR_ALIAS`
to submit a durable request. Read it with `python -m cli.runtime control-status
--account-id ACCOUNT --mode paper_broker --command-id REQUEST`.

Both commands print the durable JSON status without loading `.env` or constructing
a local runtime. Submission success means the request was stored; inspect `state`,
`applied`, and observation timestamps for its outcome. Reuse the same command ID
and exact payload for an uncertain submission retry. Conflicting payloads, missing
status, unavailable storage and invalid arguments exit with code 2. The operator
alias is request metadata and grants no authorization. An actual controller worker
is still required to execute queued requests.
