# Broker-paper qualification

## Status and authority

This runbook is an operator procedure for collecting real broker-paper evidence. It does
not authorize an account, supply credentials, approve caps, or claim that Agenthedge is
paper qualified. Software tests and synthetic transport drills prove their asserted code
paths only. Dependable paper requires five complete, clean, observed XNYS sessions for
one exact release identity and one independently configured paper account.

Stop before broker access until an owner and an independent reviewer have recorded all
of the following outside Git:

- the exact paper account ID and `paper_broker` mode;
- the client-order ownership prefix and confirmation that no unknown external order will
  be adopted or cancelled;
- an explicit US-equity/ETF allowlist and accepted whole-share, long-only capital,
  instrument, sector, gross, order-notional, order-share and position caps;
- the policy for retaining positions and cancelling owned orders at close;
- current provider and broker entitlements, with credentials supplied only through the
  named process environment;
- the exact 40-character code SHA and SHA-256 configuration, policy, strategy and data
  hashes; and
- approved issuer keys, trust file and signed candidate dossier stored outside the
  checkout.

There is no default account, universe, cap or credential. A missing item is a hold. Do
not switch accounts, relax a cap or replace missing evidence with a fixture.

## Qualification boundary

The signed dossier must satisfy `paper_start`: G0-G2, exact identity, and a current
preflight artifact. The controller supplies the expected identity, issuer keyring and
paper qualification account independently of the candidate dossier. A bare SHA-256,
boolean readiness flag, historical paper packet or local test result is not issuer
authentication.

The `dependable_paper` decision requires G0-G2 and G4 plus five session records accepted
by `ops.release_gate.evaluate_release`. Each session must bind the same qualification
account, release identity and XNYS date; span the actual venue open and close; follow the
safety qualification time; and carry a matching source-backed closeout hash. It must be
complete, clean and observed, with no reconciliation mismatch or unresolved owned order.
A no-trade day may count as an availability session, but it contributes no trade sample.

The checked-in `platform_release.json` mirrors the code policy. The Python release-gate
consumers accept trust explicitly. Current historical review-board CLI entry points do
not load independent trust, so their reports are descriptive intake and must not be used
as the canonical signed decision.

## Capture a read-only qualification preflight

After the owner/reviewer prerequisites above, use the current release's separate
collector before considering worker construction. It does not construct or activate a
worker, submit/cancel orders, access the journal, change limits, or load `.env`. It reads
only the explicitly supplied process environment and approved files. No live credentials
are required for its tests; all test responses, accounts and signing keys are synthetic.

Run from the clean approved checkout with the same reviewed runtime configuration,
strategy and data descriptor used for that release. The collector binds the actual SHA,
source cleanliness, configuration hash, strategy bytes, and data descriptor bytes to
the independent trust identity. The policy hash remains the owner's declared identity;
this probe does not validate risk/research coverage or replace other G0-G2 checks.
The trust format is documented in [worker-authority.md](worker-authority.md). Its issuer
key environment references must already exist; the collector never provisions keys or
derives trust from candidate evidence.

This bounded collector supports the current `alpaca_iex` runtime descriptor only. It
compares its provider configuration and approved quote policy with the process settings,
and probes both latest quotes and trades for every strategy symbol using explicit
`feed=iex`. Unsupported providers, unavailable subscriptions, wrong symbols, stale or
future data, invalid prices and excessive spreads produce a hold; it never substitutes
SIP, cached data or another provider. It does not claim readiness of news/macro providers
or historical research data. Outside trading hours, stale IEX data can correctly hold
the preflight even when authentication succeeds.

Supply `EXECUTION_MODE=paper_broker`, `ALPACA_PAPER_BASE_URL=https://paper-api.alpaca.markets`,
and the existing paper-only `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` variables without
printing their values. The authenticated account UUID must exactly match the independent
trust account; an account number is not silently substituted. Choose and record explicit
clock budgets with the reviewer; both must be finite, positive and at most 60 seconds.
These are observation thresholds and do not modify any trading limit.

```powershell
poetry run python -m cli.paper_qualification_preflight `
  --trust-file C:\approved\paper-trust.json `
  --checkout C:\installed\Agenthedge `
  --strategy-file C:\approved\strategy.json `
  --data-file C:\approved\runtime-data.json `
  --artifact-dir C:\approved\preflight-archive `
  --issuer APPROVED_ISSUER_ALIAS `
  --max-clock-skew-seconds REVIEWED_SKEW_BUDGET `
  --max-round-trip-seconds REVIEWED_RTT_BUDGET
```

Replace the uppercase budget/issuer placeholders with reviewed values; the command
has no implicit clock tolerance. The archive directory must already exist outside the
checkout and be access-controlled by the operator. All network requests are bounded,
authenticated GETs to fixed Alpaca paper-account/clock and data URLs with redirects
disabled. A 401/403, rate limit, timeout, malformed response or calendar outage blocks
the observation. Provider errors and raw responses are never printed or persisted.

The future timestamp is the broker clock's actual `next_open`, checked against the next
XNYS calendar opening, including holidays and DST, and again against capture completion.
It is a planning timestamp, not a fabricated future trade or an observed session. Host
skew is measured against the authenticated broker's `timestamp`: server minus the
midpoint of local UTC send/receive timestamps. The packet records monotonic round-trip
time, uncertainty, and a conservative absolute-skew bound. Excessive latency, host-clock
jumps or a skew bound above the approved budget fail closed. This is a broker-relative
clock measurement, not independent NTP certification of the broker's own clock.

Exit zero means the captured observations passed at capture time; it grants no activation
authority. Any hold exits nonzero. Once trust and release inputs validate, failed provider
probes also produce a signed hold packet with sanitized blocker codes. Invalid trust,
release binding, timestamps or output writes can fail without a packet; stdout reports
HOLD and must never be treated as qualification. Review `payload.blockers` and retain
failed captures alongside successful ones.

The resulting `paper_qualification_preflight_<sha256>.json` contains a separate observation
envelope authenticated with the existing HMAC-SHA256 issuer contract. HMAC is shared-secret
issuer authentication, not an asymmetric signature or proof of human review. Source
response hashes and selected safe fields are signed with the exact release identity.
Creation is exclusive, flushed, fsynced and read back; existing files are never overwritten.
`ops.paper_preflight.verify_preflight` verifies signature, independent identity and the
300-second observation envelope age, and rejects a passed packet once its planned opening
has arrived. Verification of a signed hold authenticates the hold, not readiness: inspect
`payload.passed` separately. Fresh quotes must be recollected for subsequent trading checks.

Local exclusive creation and signatures provide application-level immutability and tamper
detection, not OS-level WORM protection against a privileged writer. Preserve exact signed
bytes in the approved external write-once retention system. Never edit, retimestamp or
overwrite a packet. The observation envelope intentionally fails `release_decision` as a
standalone dossier. An approved issuer must still review the sources and all other G0-G2
evidence before separately issuing `current_preflight` in a release dossier. This command
does not publish or renew a worker's active evidence file, authorize an account, validate
existing holdings, or count toward the five observed paper sessions.

## Prepare the worker

Use a clean checkout at the approved SHA and an existing schema-v6 journal/control-v1
database for the exact paper namespace. Keep performance, audit and report paths absolute
and distinct. `worker-run` does not load `.env`, create schemas, create accounts or submit
a start command.

Set the reviewed process environment, including `POSTGRES_DSN`,
`RUNTIME_BACKEND=postgres`, `PORTFOLIO_ACCOUNT_ID`, `RUNTIME_NAME`,
`EXECUTION_MODE=paper_broker`, required provider variables, paper broker variables, and
the issuer-key variables named by the trust file. Do not print their values.

Run one construction iteration while no start is pending:

```powershell
poetry run python -m cli.runtime worker-run `
  --trust-file C:\approved\paper-trust.json `
  --evidence-file C:\approved\paper-evidence.json `
  --checkout C:\installed\Agenthedge `
  --strategy-file C:\approved\strategy.json `
  --data-file C:\approved\runtime-data.json `
  --performance-file C:\agenthedge-state\paper-performance.json `
  --audit-file C:\agenthedge-state\paper-audit.jsonl `
  --report-directory C:\agenthedge-state\paper-reports `
  --instance-id PAPER_WORKER_INSTANCE `
  --max-iterations 1 --poll-seconds 1
```

An `idle` result proves construction only. Require exact account/mode/SHA readback and no
unexpected open order or position before proceeding. Any unavailable database, provider,
calendar, reconciliation, release or observer input is a recorded blocked preflight.

The one-iteration construction command exits and stops its Runtime. After its lease
expires, launch the same reviewed command with a new instance ID and a positive
`--max-iterations` budget that keeps one process running from before the opening
coverage window through closing readback. Choose the budget with the approved poll
interval and actual XNYS session schedule; keep that process under supervision.
Do not repeatedly invoke the one-iteration example to run a session: process restart
does not resume a prior start command. Submit the commands below from a separate
terminal while the session worker remains running.

## Run one observed session

Use unique stable command IDs. Submit the preflight before the same-day XNYS open, within
the approved boundary grace:

```powershell
poetry run python -m cli.runtime control-submit `
  --account-id PAPER_ACCOUNT --mode paper_broker `
  --command-id SESSION_PREFLIGHT_ID --action reconcile `
  --release RELEASE_SHA --actor OPERATOR_ALIAS

poetry run python -m cli.runtime control-status `
  --account-id PAPER_ACCOUNT --mode paper_broker `
  --command-id SESSION_PREFLIGHT_ID
```

Pump the reviewed worker and require `state=succeeded`, `applied=true`,
`preflight_qualified=true`, and retained `session_coverage`. A successful reconciliation
outside the opening window does not qualify coverage.

At or after the venue open, submit the paper start and keep the same fenced worker running
through the session:

```powershell
poetry run python -m cli.runtime control-submit `
  --account-id PAPER_ACCOUNT --mode paper_broker `
  --command-id SESSION_START_ID --action start_paper `
  --release RELEASE_SHA --actor OPERATOR_ALIAS
```

Keep the same worker's signed dossier current using the
[evidence renewal procedure](worker-authority.md#renew-evidence-during-a-running-session).
The current-preflight artifact has a 300-second maximum age. Collect and sign fresh
observations before that limit; a renewal failure stops new ticks and requires
recovery review. Replacing a file does not automatically resume an interrupted session.

Only an owner-approved paper order within the recorded allowlist and caps may be exercised.
Observe the venue's actual accepted, partial, filled, cancelled or rejected states and
reconcile actual economic activity. Do not manufacture a partial fill. Label synthetic
rare-event tests separately. Keep periodic durable status, audit, alert, provider-age,
clock, order and reconciliation observations without credential values.

At or after the reviewed XNYS close, let the worker record the closing observation, then
submit and pump the close command:

```powershell
poetry run python -m cli.runtime control-submit `
  --account-id PAPER_ACCOUNT --mode paper_broker `
  --command-id SESSION_CLOSE_ID --action close_session `
  --release RELEASE_SHA --actor OPERATOR_ALIAS

poetry run python -m cli.runtime control-status `
  --account-id PAPER_ACCOUNT --mode paper_broker `
  --command-id SESSION_CLOSE_ID
```

Accept the day only when the readback is `succeeded` and `CLOSED`, the source-backed
closeout hash is present, reconciliation is complete at the same journal revision, and
owned open/unresolved orders are empty. Read back residual positions and cash against the
owner's retain-position policy. A midday halt, pending close, recovery-required command,
stale lease or reconstructed timestamp is not a completed session.

## Five-session decision

Retain a sanitized index to the five accepted session dossiers and their
underlying controller, journal, reconciliation, audit and broker records. Record no-trade
days honestly. Preserve every failed or interrupted day with its reason; do not remove it
to improve the window. A material repair to execution, accounting, risk, strategy, data,
release policy or identity invalidates the affected observations and starts a new matching
window.

The evidence issuer reviews the underlying records, embeds the five closeouts under the
same identity, signs the dossier, and evaluates it for `dependable_paper`. Reviewer and
tester record an explicit go/hold outcome and residual risks. Store credentials, account
secrets, raw provider payloads and capital details outside Git; commit only sanitized
references.

Current repository state supplies software contracts and synthetic verification. Until
the actual account, caps, credentials, authorization and five observed sessions exist,
the truthful result is **hold: paper qualification incomplete**.
