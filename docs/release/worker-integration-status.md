# Worker integration status

The installed worker, closeout and recovery implementation is accepted in merged
software baseline `b01544cbdf8ecf2e36a8506763dbc2119b7bb9b4`. Its full tracked tree
equals operator head `741c8ba` (tree `8d1d3e7`). Relative to reviewed freeze
`4e0dcfa78a12f7c2fbe5c5b2da662980dc0bedd7`, production source, configuration,
scripts and workflows are unchanged; one reviewed CLI test and three evidence
documents differ.
Combined-main post-merge checks all passed:
[quality 34998745169](https://github.com/Jeduardo622/Agenthedge/actions/runs/34998745169),
[staged 34998745153](https://github.com/Jeduardo622/Agenthedge/actions/runs/34998745153),
1,863 tests collected and executed, zero deselections or collection skips, 87.32%
coverage in 330.97s. A later documentation-only closeout commit grants no runtime authority. G0 independently binds
the actual deployed SHA with configuration, policy, strategy, data and issuer evidence.

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

## Verified workflow follow-through

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
- Freeze `4e0dcfa` passed 1,862 tests with zero skips on Windows and Linux at
  87.35%/87.32% coverage. Mypy checked 150 source files; lint, lock, build, package
  smoke and installed-dependency audit passed; the unpublished local package was
  excluded. Core, operator exact-head and combined-main post-merge checks passed.

Missing live account authorization, provider credentials/entitlements and observed
market sessions remain external qualification prerequisites. No broker or live
qualification is claimed by the isolated synthetic tests.
