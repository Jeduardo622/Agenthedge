# Bounded SPY paper experiment

The optional `paper_mandate` strategy-artifact field binds the approved experiment
to a dedicated, initially empty paper account. It grants no release authority.
Normal independent G0–G2 approval, current evidence, reconciliation, session
controls, worker fencing and execution safety remain required.

## Explicit preparation

Use a new account UUID and its actual broker cash/empty inventory for the journal
genesis. Never use the experiment allocation as invented broker cash. Initialize
truthful reconciliation coverage, then explicitly call
`PostgresJournal.install_paper_mandate(account_id, "paper_broker", mandate)`.
This writes the immutable mandate hash before the first intent. Repeat installation
is idempotent only for the same hash. Construction and reads require the existing
binding; they never install or adopt a position.

The manifest supplies every `PaperMandate` field: `account_id`, `allocation`,
`max_order_shares`, `max_order_notional`, `max_position_shares`,
`max_position_notional`, `max_instrument_fraction`, `max_sector_fraction`,
`max_gross_fraction`, `max_outstanding_orders`, `symbol`, and `strategy`.
Both direct construction and JSON parsing validate ceilings. The bounded contract
is SPY/Momentum, allocation at most $10,000, one share/$1,000 per order and
position, instrument/gross fractions at most 0.10, sector at most 0.25, and one
outstanding order. Separately configure the existing execution safety limits;
the installed artifact must bind all effective agent parameters.

## Economics and loss controls

The full-account journal remains authoritative. The experiment projection uses
its allocation plus owned economic changes, excluding external transfers; every
trade must trace to an intent with the installed mandate hash. Unknown inventory
or positive non-transfer income without qualified attribution requires recovery.
It cannot be silently credited to the experiment. Pending/unknown orders retain
their reservations until canonical reconciliation proves terminal economics.

Session controls persist account and experiment baselines/marks under the same
account lock and journal revision. Experiment warnings start at 1%; pause and
hard halt at 2% and 5%. Account policy uses account equity independently. Their
actions combine so either stricter dollar limit blocks exposure. Experiment
returns do not replace account returns in evidence. Restart cannot reset either
baseline or remove the mandate. Warning observations use the configured alert
notifier and durable report paths; they do not establish unattended monitoring.

## Data and release boundaries

The experiment requires the explicit authenticated IEX descriptor described in
[IEX runtime](iex-runtime.md). Preserve each price's trade timestamp separately
from the bid/ask timestamp, same-feed prior close/history/liquidity, and validated
ETF look-through provenance. A final capture never widens the approved limit;
the exact capture must still be fresh after blocking authorization checks.

Before control readback can reconcile a completed tick, the worker must finish
downstream events emitted by that tick's handlers. The existing configured bus
deadline covers the complete chain; each downstream event does not receive a new
timeout budget. Expiry retains the existing fail-closed halt and recovery path.

Tests using synthetic transport/signatures are software verification only. A
passing test suite does not create a new broker account, approve operational
evidence, count an observed session, or establish paper/live qualification.
