# Worker authority files

The owner manages the trust file separately from a candidate release dossier.
Do not generate the trusted identity or issuer list from the candidate's claims.
`load_worker_authority` reads both explicit paths without loading `.env`, creating
a Runtime, connecting to a provider, or changing persistent state.

The trust JSON requires `schema_version: 1`, the complete `ReleaseIdentity` in
`identity`, `paper_account_id`, and `issuer_key_environment`. The latter maps each
approved issuer name to the name of an existing environment variable containing
its HMAC key. Keys must contain at least 32 bytes; their values never belong in
the trust file, command output, or a committed example. The variable names are
operator-supplied references, not new application defaults.

The candidate JSON is the signed release-gate envelope. Loading expired evidence
is permitted for recovery inspection. `WorkerAuthority.check(now=...)` still
requires the current stage's signed evidence before any start. Loading these
files by itself does not start a worker or authorize a broker request.

Session controls require all five fields: `max_mark_age_seconds`,
`boundary_grace_seconds`, `window_sessions`, `max_drawdown`, and
`control_timeout_seconds`. There are no implicit policy defaults in this parser.
The installed worker's approved strategy artifact must bind these settings before
using them in a session observer. Construction alone grants no trading authority.

## Renew evidence during a running session

The installed worker binds the owner-selected `--evidence-file` path at construction
and rereads it before active iterations. Keep this path outside the checkout. Renew
the dossier before its expiry or the current-preflight artifact's 300-second maximum
age, allowing time for issuer review, publication and the next worker iteration.

1. Collect a new actual current-preflight observation and retain its source records.
   The approved issuer verifies the records and signs a replacement with the same
   exact account, mode, code/configuration/policy/strategy/data identity and gate
   policy. Changed evidence requires a strictly newer aware `issued_at`; never
   change an old observation's timestamp to make it fresh.
2. Write the complete signed JSON to a temporary file in the same approved directory,
   validate its signature and stage decision using the independently configured trust,
   then atomically replace the original evidence file. Keep the prior signed dossier
   in the protected evidence archive. Do not edit the active file incrementally.
3. Observe the next worker iteration and fresh durable running receipt. A valid
   renewal preserves the current agents, accepted strategy state and worker lease.
   It does not authorize another account, a different policy or an automatic start.

A missing, partial, stale, incorrectly signed or mismatched replacement blocks new
ticks and sends and makes the running command recovery-required. Publishing corrected
evidence does not resume a worker after this interruption, an explicit halt or loss
of its lease. Resolve the recorded recovery state and use the existing authorized
start/rearm procedure; preserve emergency/risk/compliance halt restrictions. A source
or release-identity change requires a newly reviewed worker installation.
