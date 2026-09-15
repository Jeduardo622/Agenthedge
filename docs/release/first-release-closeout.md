# First owner-operated release closeout

**Decision: software verification complete; operational qualification remains on
hold.** This is the current implementation record, not
permission to use a broker account or enable live trading.
The historical 2025 phase checklists do not establish acceptance of this release.

## Software and evidence

The software acceptance baseline is merged main
`b01544cbdf8ecf2e36a8506763dbc2119b7bb9b4`. [PR52](https://github.com/Jeduardo622/Agenthedge/pull/52)
merged the reviewed core as `fef4d9d`; [PR53](https://github.com/Jeduardo622/Agenthedge/pull/53)
merged the operator layer as `b01544c`. Its full tracked tree equals exact operator
head `741c8ba` (tree `8d1d3e7`); relative to reviewed freeze `4e0dcfa`, production
source, configuration, scripts and workflows are unchanged, while one reviewed CLI
test and three evidence documents differ. Later documentation commits grant no runtime
authority; G0 independently binds the actual deployed SHA and other identities.

- Local freeze `4e0dcfa`: Windows 1,862 passed, zero skips, 87.35% coverage in
  915.46s; Linux 1,862 passed, zero skips, 87.32% coverage in 413.29s. Linux retained
  complete output and exit proof after its structured tmpfs artifact was lost.
- Static verification passed: mypy checked 150 source files, lint and lock checks
  passed, the package built and passed smoke, and the installed-dependency audit found
  no known vulnerabilities; the unpublished local package was excluded. The retained
  report is `agenthedge-31e-review/.cache/completion/final-harness/FINAL-VERIFICATION.md`
  (SHA-256 `4DDFF2E47F220EC95045CE7C58B97C56A8CDF79B836092A50878C108AB4785B1`).
- Core post-merge checks all passed: [quality run 34997797445](https://github.com/Jeduardo622/Agenthedge/actions/runs/34997797445),
  [staged run 34997797597](https://github.com/Jeduardo622/Agenthedge/actions/runs/34997797597),
  1,837 tests, zero skips and 87.24% coverage.
- Operator exact-head checks all passed at `741c8ba`:
  [quality run 34997833866](https://github.com/Jeduardo622/Agenthedge/actions/runs/34997833866),
  [staged run 34997833777](https://github.com/Jeduardo622/Agenthedge/actions/runs/34997833777),
  1,863 tests, zero skips and 87.32% coverage in 315.38s.
- Combined-main `b01544c` post-merge checks all passed:
  [quality run 34998745169](https://github.com/Jeduardo622/Agenthedge/actions/runs/34998745169),
  [staged run 34998745153](https://github.com/Jeduardo622/Agenthedge/actions/runs/34998745153),
  1,863 collected and executed tests, zero deselections or collection skips, 87.32%
  coverage in 330.97s. Mypy checked 150 source files; lint, package, Cosign
  verification and the installed-dependency audit passed.
- CLI test-only repair `39f6a60` / core `85f5c1f` / integration `d117119` changed no
  production code and passed its three focused tests on Windows and Linux plus root's
  two-case check.
- Independent review was split across the lead and three non-author reviewers:
  `baseline_tester` covered baseline/test isolation and cross-stream review, `e1`
  covered execution/replay and cross-platform proof, and `e3` covered operations,
  fencing and adversarial lifecycle probes.
- [Browser evidence](operator-browser-verification.md) verifies actual loopback
  controls and parsed export content. Broker transport, clocks and accounts were
  synthetic; this is software evidence, not an observed broker session.

## G0-G6 acceptance

| Gate | Current decision | Missing acceptance |
| --- | --- | --- |
| G0 | Hold | Owner-recorded exact release/config/policy/strategy/data identities and mandate, plus an approved issuer-signed matching dossier |
| G1 | Hold | Approved issuer attestation binding the accepted accounting, crash/replay, reconciliation and migration/restore evidence |
| G2 | Hold | Current redacted provider/broker/account preflight for the exact paper identity: ownership prefix, no unknown external orders, complete reconciliation, venue clock, alerts and signed G2 artifacts |
| G3 | Hold | Owner-approved strategy objective, representative causal frozen-holdout results and independent strategy acceptance for the exact strategy/data hashes |
| G4 | Hold | Five complete, clean, source-observed XNYS paper sessions for one exact identity/account, each with matching closeout and no unresolved order or mismatch |
| G5 | Hold | Twenty representative accepted paper sessions, actual account readiness, approved universe/caps, credentials/entitlements, owner authorization, retention/rollback policy and matching observed fault drills |
| G6 | Hold | Owner-authorized staffed supervised live pilot, actual halt/closeout/operator/restore outcome and independent residual-risk acceptance |

Canonical stage policy remains `paper_start=G0-G2`,
`dependable_paper=G0-G2+G4`, `live_start=G0-G5`, and `closeout=G0-G6`.
Synthetic no-trade sessions cannot satisfy observed broker-session requirements.
No-trade observed days can prove availability, but add no trade sample.

## Remaining work

Obtain the actual account, approved universe/caps,
provider/broker entitlements, operator and issuer authority outside Git before
starting broker-paper qualification. Preserve failed or interrupted session records;
material repairs invalidate the affected evidence. Do not substitute another account
or fixture when these prerequisites are missing.

**Software-complete: yes. Paper-qualified: no. Live-pilot-qualified: no.**
The first release is bounded to the [owner mandate](mandate.md). Shorts, derivatives,
FX, crypto, additional venues/currencies, customer accounts and autonomous upward
promotion remain later milestones; this closeout does not complete the original
multi-asset program.
