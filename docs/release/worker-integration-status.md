# Worker integration status

The installed worker, closeout and recovery slices are integrated through
`f6d0bbcd1d78ed67b5373f467f7cd66bccbe97a4`. This is not an accepted operator release
and has not been published. Full milestone verification remains in progress.

Implemented boundaries include explicit paused binding before startup, actual
Runtime start/halt readback, command recovery by observation, separate paper
rollback requests and fresh worker-bound running receipts. Current failed readback
invalidates an older running receipt immediately; raw exception text is excluded.

## Reviewed and integrated

- Canonical provider quotes retain event/availability timestamps; approved PIT
  research and provider configuration are bound separately. Opening baselines
  use exact-opening observations, with current marks used for subsequent risk.
- Exact clean installed source, strategy manifest, policy/data identities,
  consumed agent dependencies and observer/provenance configuration are checked
  before setup and later authority checks. Mutation revokes authorization.
- Explicit signed strategy installation respects the actual account tracker and
  preserves recorded safety reductions. Worker construction grants no authority.
- Pre-open controller coverage and closing valuations form source-backed session
  closeouts; atomic publication verifies the current economic/reconciliation
  revision and completed halt. Midday halt cannot report a complete session.
- Root and independent reviewer each passed all nine installed-worker/closeout
  PostgreSQL tests. Actual builder-to-six-agent tests passed independently and
  root's combined builder suite passed15 tests23.67s. Synthetic transports remain
  clearly separated from broker qualification.

## Required follow-through

- Installed start/halt/partial/late-fill/restart and separate-paper rollback
  fixtures have passed independent PostgreSQL verification. Ordinary post-close
  rearm is integrated; emergency/risk/compliance/recovery latches remain blocked.
- Actual browser preflight/start/closeout, simulation start/stop, inherited-order
  halt/late-fill/restart and expired-lease controls are recorded in
  [browser evidence](operator-browser-verification.md). The stale reservation display
  correction is independently reviewed, integrated at17a978c and browser-confirmed.
- Same-worker signed-evidence renewal is reviewed and integrated at0712a25;
  independent47 tests196.83s passed with zero skips. Invalid renewal revokes final
  authorization and cannot restart an interrupted worker through background refresh.
- Complete full Windows/Linux milestone verification, reviewed publication, CI
  and post-merge checks. The test-owned bus cleanup is integrated at217dc28; its
  decisive197-test sequence passed independently on Windows and Linux.

Missing live account authorization, provider credentials/entitlements and observed
market sessions remain external qualification prerequisites. No broker or live
qualification is claimed by the isolated synthetic tests.
