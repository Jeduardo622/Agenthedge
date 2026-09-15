# Durable operator workflow verification

## Scope and environment

Verified through the Codex Computer Use browser on 2026-09-15, against a local-only
Streamlit dashboard at `127.0.0.1:8517` and the actual installed worker at commit
`ea39319ef4edb77c9b69fd682cd5b818cd7c2f02`. The worker used disposable PostgreSQL,
six real agents, installed artifact checks and synthetic broker/provider transport.
The controlled clock advanced through September 14. This is software workflow
evidence, **not an observed market session or broker qualification**.

Task fixture and readbacks are retained under
`agenthedge-browser-fixture/.cache/completion/browser-state/`; setup is documented
in that worktree's `.cache/completion/browser-fixture.md`. Screenshots and browser
accessibility observations are retained in the Codex task conversation.

## Observed workflow

| Action | Observed result |
| --- | --- |
| Open dashboard before session | Cash1000, no positions/orders/economic activity; worker had zero agents/ticks/submissions |
| Submit reconcile before open | PENDING until an actual worker iteration; repeated click reused the same command |
| Process preflight and refresh | SUCCEEDED; persisted exact release identity and original pre-open coverage timestamps |
| Submit explicit paper start | Actual worker started six agents and performed one permitted tick; browser displayed SUCCEEDED |
| Reload browser | Same durable command history and account balance; worker remained at one tick, with no extra submission |
| Observe closing time and submit close | Actual completed halt and CLOSED source-backed artifact; cash1000 and trade_count0 |
| Export bounded snapshot | Browser download event observed; keyboard activation of the existing close request retained the same command |
| Narrow viewport390x844 | Heading, account identity and controls wrapped within viewport; table columns require horizontal scrolling |
| Stop fixture worker and wait for lease expiry | Browser displayed unavailable-worker warning; submit button independently read as disabled |

Command IDs:

- Preflight: `f72838bb-a985-4d86-98fa-8d9b3920d524`
- Start: `accccc8d-cf38-45bd-9ed2-f2748d06767d`
- Close: `bc83a1f1-2d14-46bf-aadb-3e22eacc3af1`

The final durable snapshot contained exactly these three commands. Closeout hash:
`c561c8a381877c7a4c5d19c9026752ade947852bad0ffcde7840701f075ff589`.
Controller coverage began `2026-09-14T13:29:00.000012+00:00`; closing observation
was `2026-09-14T20:00:00.000061+00:00`. Final worker counters were six agents,
one tick and zero broker submissions.

## Findings and remaining proof

The old start remained `recovery_required` after the completed close and would
block a subsequent start. This was reproduced in a maintained PostgreSQL test;
the bounded settlement/read-model correction is under independent review.
Post-close rearm is separately implemented and awaits actual restart integration.

This browser pass does not cover a fake partial-fill/cancel/late-fill lifecycle,
same-account worker takeover, or simulation start/stop. Those planned workflows
remain outstanding. Export download initiation was observed; this pass did not
read the browser-managed downloaded file. The retained server snapshot is available
for exact field comparison. Dense raw risk/reconciliation details remain a usability
limitation; they are not evidence of successful controller operation by themselves.

## Follow-through: simulation workflow and corrected missing-risk display

The closeout recovery and rearm findings above are now reviewed and integrated at
`fc94e077707a22872d04ee4dfa05965eb94bcc5f`; actual restart acceptance is recorded
in the implementation tracker.

Actual Computer Use browser verification used loopback8518 in a separate process,
with `dashboard_environment`, task-only state paths, no supplied provider/broker
credentials, disabled dotenv and external HTTP blocked. The actual dashboard and
Runtime were used. Start transitioned Stopped -> Starting -> Running; one tick
completed with six agents and a visible Director failure because Finnhub was not
configured. No market-data success or simulated trade was claimed.

The browser reload retained the same last-update timestamp
`2026-09-15T15:01:56.358680+00:00`, one completed tick and $1,000,000 simulated
cash. Stop transitioned to Stopped, enabled Start and disabled Stop, while retaining
the cash and last completed observation. Source at this initial pass was `ea39319`.

This missing-provider scenario exposed zero NAV/VaR defaults. Reviewed correction
`49af4508d7fde5c10f1b4db65d9588f7301c708a` is integrated as `3775f39` and was
browser-verified at fixture SHA `5e506c2`: NAV, gross exposure, leverage, VaR and
drawdown all display Unavailable; cash remains $1,000,000. Empty provider results
show the no-providers message only after the check completes. Root independently
passed17 focused dashboard/helper tests2.52s, including real observed zero values.

Screenshots are retained inline in the task's Computer Use evidence. No local PNG
artifact path is claimed. The fake-order/late-fill/restart browser fixture remains
the next verification flow; browser-managed download contents remain unverified.

## Follow-through: inherited partial order, halt, late fill and restart

Actual Computer Use verification subsequently exercised loopback8519 at installed
worker SHA `b554986f101c6ed7fb13befea95dea0dd65f9061`. The disposable phase3 database
was `browser_order_e3`, account `release-82f3515e8e41462ba74668030a13855c`, mode
`paper_broker`. The installed builder, six agents, journal, controller and dashboard
were real. All broker responses and the clock were explicitly synthetic; external
HTTP was blocked. An inherited accepted two-share SPY order was seeded before
worker binding. This verifies recovery, not admission of a new broker order.

| Action | Browser and controller observation |
| --- | --- |
| Initial snapshot | Cash1000; accepted two-share order with reserved cash200 |
| Reconcile inherited partial | Cash900, one SPY share, one remaining share and reserved cash100 |
| Explicit paper start | Six agents and one tick; durable start succeeded |
| Halt | HALTING, pending cancellation and prominent recovery/outcome-uncertain status; exactly one synthetic DELETE |
| Deliver synthetic late half-share fill and cancellation | Cash850,1.5 SPY shares, canceled order and exactly two stable economic activity IDs |
| Complete halt and refresh | HALTED; original halt command SUCCEEDED with controller observation |
| Stop old Runtime and construct a new Runtime on the same durable state | Two recovery iterations retained cash850,1.5 shares, both activity IDs and all three commands; zero new agents/ticks and no additional cancellation |

Command IDs were reconcile `a774c3d1-e85e-4edd-8ac6-7429b278e0a1`, start
`4dabc8d3-e487-4709-b3a3-cab1a3fe7e69`, and halt
`22dd3d53-89a0-4f92-8625-ba80fb348895`. Economic activity IDs were
`partial-activity` and `late-activity`. Final worker readback reported generation1,
zero ticks/agents in the restarted instance, one cancellation and no last error.
The task driver was stopped through its tenth command, `010-stop.json`.

The browser exposed a remaining display defect: after reconciliation proved the
cancellation complete and the journal had no active reservations, the order table
still displayed historical reserved cash50. Root reproduced this with PostgreSQL
and AppTest, implemented a bounded current-reservation read-model correction at
`24a0de5e98327c92dd3853a5ba6553b83fd6fd0a`, and passed52 affected tests45.54s.
Independent e3 review approved the exact correction and passed12 fresh PostgreSQL
dashboard/view tests14.03s. It is integrated as `17a978c`.

Browser confirmation used fixture SHA `378886ef7cd3f33ed5ad2f8785cc1331573c63c1`
against the retained database after the broker worker was stopped. Streamlit's
existing process initially retained an older imported read-model module, displaying
Unavailable after hot reload. Restarting only the read-only dashboard loaded the
complete reviewed correction: the canceled row displayed reserved cash0, filled1.5,
fill value150 and economic gapfalse. Cash850, the1.5-share position and both events
were retained. The expired worker lease disabled command submission. This display
check did not renew worker authority or restart execution under changed source.

Task-only command, state and snapshot evidence remains in the isolated
`agenthedge-installed-order-recovery/.cache/completion/browser-order-state/`.
Screenshots are inline in the Codex task. No real broker fill, cancellation,
qualification session or live operation occurred.
