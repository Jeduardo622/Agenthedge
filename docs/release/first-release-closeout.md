# First owner-operated release closeout

**Decision: blocked; completion review remains open.** This is the current
implementation record, not permission to use a broker account or enable live trading.
The historical 2025 phase checklists do not establish acceptance of this release.

## Software and evidence

Reviewed integration is on `codex/platform-completion`; the current task-by-task
[tracker](implementation-tracker.md) records exact component SHAs and reviewers.
The installed worker/restart/rollback milestone is
`fc94e077707a22872d04ee4dfa05965eb94bcc5f`; the reviewed unavailable-risk display
repair is integrated at `3775f39`. These are intermediate milestones, not the
final accepted release SHA. Current publication/CI/post-merge evidence is pending.

- Last completely green aggregate: `92b36f307224da675476bd7784708a43cac2b60f`,
  Windows1621 passed, zero skips,87.11% coverage; Linux1621 passed, zero skips,
  87.07%. Later worker changes require the new aggregate run.
- Reviewed rearm candidate `d599c4a`: root35 PostgreSQL tests passed161.37s;
  independent reviewer17 passed110.41s. Ordinary close can rearm only with
  current signed authority, exact reconciliation/session state and an active
  lease; emergency, risk and recovery halts remain blocked.
- Independent actual separate-worker rollback tests preserve live holdings and
  require the exact linked paper receipt. Root's broker fixtures never contact
  an external broker; these are software tests, not actual observed sessions.
- [Browser evidence](operator-browser-verification.md) records actual UI control
  IDs and durable readback, including inherited-order halt/late-fill/restart and
  corrected current reservations. The browser export JSON response passed 12
  parsed identity/economic/control assertions; its filesystem download location
  remains unverified. The broker transport and clock were synthetic.
- Frozen `31e9a94` passed1794 Windows tests, zero skips,87% coverage in676.27s,
  plus mypy149 files, flake8, lock check, build, package smoke and dependency audit.
  Its Linux aggregate exposed leaked test threads. Reviewed cleanup `b4932295`
  passed the decisive197-test sequence on Linux47.33s and independently on
  Windows25.14s; it is integrated at `217dc28`. Final combined verification remains
  pending. Failed setup and test runs are retained as failures in the tracker.
- The reviewed PostgreSQL runner `3cd1eca` is integrated through `d67e1f0` and
  independently passed21 focused tests32.32s. Full local and exact-head hosted CI
  must still pass. Renewal `0e67b68` is reviewed and integrated at `0712a25`:
  independent47 tests196.83s passed with zero skips, including actual PostgreSQL
  workers and invalid-evidence recovery probes. The installed order-restart
  compatibility and final-guard regression passed 3 tests in 36.21s with zero skips at
  clean `4e0dcfa`. Final aggregate, publication and exact-head CI remain pending.

## G0-G6 acceptance

| Gate | Current decision | Missing acceptance |
| --- | --- | --- |
| G0 | Hold | Exact final release/config/policy/strategy/data identities, owner mandate and issuer dossier |
| G1 | Hold | Final combined software checks and matching signed accounting/recovery/migration artifacts |
| G2 | Hold | Remaining installed workflows, sustained signed-evidence renewal, final safety checks and current account preflight |
| G3 | Hold | Actual approved strategy objective, representative causal holdout results and independent strategy acceptance |
| G4 | Hold | Five complete clean observed paper sessions for the exact identity; none supplied as acceptance evidence |
| G5 | Hold | Twenty representative observed paper sessions, real account/caps/owner authorization and matching fault-drill evidence |
| G6 | Hold | Supervised observed live pilot, final operator/restore proof and residual-risk acceptance |

Canonical stage policy remains `paper_start=G0-G2`,
`dependable_paper=G0-G2+G4`, `live_start=G0-G5`, and `closeout=G0-G6`.
Synthetic no-trade sessions cannot satisfy observed broker-session requirements.
No-trade observed days can prove availability, but add no trade sample.

## Remaining work

Complete the executable software items and final review/verification/publication
listed in the tracker. Then obtain the actual account, approved universe/caps,
provider/broker entitlements, operator and issuer authority outside Git before
starting broker-paper qualification. Preserve failed or interrupted session records;
material repairs invalidate the affected evidence. Do not substitute another account
or fixture when these prerequisites are missing.

**Software-complete: no. Paper-qualified: no. Live-pilot-qualified: no.**
The first release is bounded to the [owner mandate](mandate.md). Shorts, derivatives,
FX, crypto, additional venues/currencies, customer accounts and autonomous upward
promotion remain later milestones; this closeout does not complete the original
multi-asset program.
