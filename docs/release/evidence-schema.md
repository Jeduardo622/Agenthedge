# Release evidence schema v1

## Status and authority

`ops.release_gate.evaluate_release` is the O3a validation core. It does not start a
worker, place an order or qualify this checkout. CLI/runtime integration, actual
issuance and observation collection remain O3b/O1/O5 work. No deployed issuer key
or observed qualification dossier has been created by this change.

The controller supplies `ReleaseIdentity`, `now`, an approved issuer keyring and,
for observed-session stages, the actual paper qualification account ID independently
of candidate evidence. Missing trust denies every stage. Never load the keyring,
expected identity or qualification account from the candidate dossier. Keys are
injected bytes of at least 32 bytes; provision real random keys outside Git and
rotate/revoke them through the trusted controller. No environment variable or file
lookup occurs in this core. Test keys and observations are synthetic fixtures.

HMAC-SHA256 authenticates an approved issuer using Python's standard `hmac`
implementation and `compare_digest`. It is a shared-secret message authentication
code, not an asymmetric signature or proof of a human review. An unkeyed SHA-256
checksum (including the older rehearsal `signature`) cannot authenticate an issuer.
The controller must ensure the issuer is authorized to attest every required check;
the issuer must inspect the underlying test, broker and operator evidence. Signing
a fabricated receipt does not turn it into an observation.

Reference: [Python HMAC documentation](https://docs.python.org/3/library/hmac.html).

## Wire format

### Consumer integration

`ReleaseTrust` carries the independently supplied expected identity, copied issuer
keyring and qualification account. `release_decision` returns only stage, passed
and reasons. The keyring is excluded from its representation and from reports.
Candidate evidence never supplies these trust settings.

`AgentRuntimeConfig.from_env` now requires authenticated `live_start` evidence in
addition to its existing live toggles and explicit caps. It compares the expected
config hash with `release_config_hash()`: SHA-256 of compact sorted-key JSON of
all parsed dataclass settings, including nested safety and governance settings.
It reads no secrets for that hash. Changing a cap or other parsed setting
invalidates the evidence. Legacy readiness booleans still parse for compatibility;
alone they now raise an explicit deprecation error instead of authorizing live.

The review board, live readiness report and switch packet expose the same
`release_gate` result in JSON and Markdown. Their historical session summaries
remain descriptive and cannot override that result. The switch fixes the stage
to `live_start`, binds the actual preflight account/mode, validates the parsed
configuration, and retains all preflight and controller blockers. It still cannot
apply a transition until the durable controller is integrated.

These Python consumers accept evidence and trust explicitly. The command-line
entry points have no trusted deployment loader yet and therefore report missing
trust. Runtime enforcement for direct dataclass construction and durable control
admission remain the sequential O3b/O1 follow-on; parsing a configuration or
producing a report is not worker authorization. No deployed trust or real broker
qualification has been created.

An envelope has `payload` and `signature`. Signature fields are `algorithm` exactly
`hmac-sha256`, `issuer`, and lowercase hexadecimal `digest`. Authenticate the UTF-8
JSON payload encoded with sorted keys, compact comma/colon separators and no
non-finite numbers. The signature is outside the signed payload.

Payload fields:

- `schema_version`: integer 1.
- `identity`: exact SHA (40 lowercase hex), account ID, mode, and four SHA-256
  hashes named config_hash, policy_hash, strategy_hash and data_hash.
- `policy_hash`: SHA-256 of canonical `release_policy()`. This binds the release
  gate rules in addition to the trading policy hash inside identity.
- `issued_at`, `expires_at`: aware timestamps; issued <= now < expires, maximum
  age and validity window 24 hours. Future issuance and naive timestamps fail.
- `gates`: gate ID to check-name/artifact-digest mappings. Every required check
  references a complete embedded artifact; a bare pass flag cannot substitute.
- `artifacts`: SHA-256 digest to artifact. Each artifact carries kind, exact release
  identity, observed_at no later than issuance, passed exactly true and nonempty
  details. Its canonical hash must match its reference. The issuer is responsible
  for retaining and validating actual underlying evidence identified by details.
- `sessions`: observed paper closeouts when the requested stage requires them.

The runtime policy is produced by `release_policy()`; the checked-in
`config/promotion-gates/platform_release.json` is a reviewable mirror enforced by
a test. Runtime and packaged installations do not depend on finding a repository
relative config file. Consumers must use this one function rather than copy rules.
The current-preflight artifact expires for admission after five minutes. This is
an explicit software policy default, not a claim about measured provider latency.

## Stage requirements

| Stage | Required gates | Complete observed paper sessions |
| --- | --- | --- |
| paper_start | G0-G2 | 0 |
| dependable_paper | G0-G2, G4 | 5 |
| live_start | G0-G5 | 20 |
| closeout | G0-G6 | 20 |

G6 is a post-pilot acceptance gate and cannot be required to begin the pilot.
Research paper admission does not first require G3 strategy profitability or a
paper history. Admission still requires G0-G2 evidence, including current preflight.
The controller must independently bind the requested action to stage and mode;
this validation core performs no mode transition.

## Session closeouts

Every session carries a unique XNYS `session_id` (ISO date), the independently
configured qualification `account_id`, mode `paper_broker`, release identity,
opened_at, closed_at and safety_qualified_at. Coverage must span the actual venue
open/close from the qualified calendar, close before issuance and start after
safety qualification. Unknown calendar dates fail closed. Each session requires
complete/clean/observed exactly true, empty mismatches/unresolved_orders and an
integer nonnegative trade_count. Its `closeout_hash` references a complete
`session_closeout` artifact whose details exactly bind all the session fields
except the reference itself and whose observation is at or after close.

The signed release identity binds the target release/account; the separate paper
account field identifies the source of qualification observations. This permits a
reviewed paper dossier to support a different live account without silently
equating their ledgers. Missing trusted account mapping blocks qualification.

Malformed, duplicate, incomplete, unobserved, wrong-account or mismatched-release
sessions block acceptance, including when enough other sessions exist. No-trade
days may count availability; G3 strategy acceptance remains a separate mandatory
live check and the gate does not count zero trades as a performance sample.
Material source/configuration/policy/strategy/data changes change identity and
invalidate the old dossier. A new signature must not backdate missing observations.

## Runtime admission and recovery

Broker runtimes take independently supplied `ReleaseTrust` and candidate evidence.
They copy the evidence and bind it to the actual account, mode, and parsed runtime
configuration. Caller agent extras cannot replace the runtime-owned store, broker,
safety configuration, or release authorization. Paper orders require `paper_start`;
live orders require `live_start`. Approval messages cannot supply trust.

Bootstrap and halted or expired-evidence loops still reconcile economic events and
unknown orders. Missing or expired evidence blocks strategy ticks and new broker
submissions, including a second check after the durable submission claim and before
HTTP. A blocked claim remains unknown until reconciliation proves its outcome.
The builder exposes explicit trust/evidence arguments; no issuer keys are inferred
from environment flags or dossier content. The durable controller must still bind
installed code, policy, strategy, and data to its independently supplied identity.
