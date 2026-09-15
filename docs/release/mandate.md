# First owner-operated release mandate

Status: implementation and local verification scope; not authorization to trade.

The first release supports one explicitly identified owner account, US-listed equities
and ETFs, USD accounting, whole-share long new-risk orders, regular exchange hours and
an explicit symbol allowlist. Account identity, allowed universe and capital caps must be
provided and accepted before broker qualification. There is no default live account.
Paper/live account and mode namespaces must remain separate in all durable identities.

The economic ledger also represents fractional corporate-action residuals exactly.
Residual reductions require a supported, explicitly authorized broker route. They are
never rounded away. Limit orders are the initial new-exposure route; no additional order
types, shorts, margin, or asset families are approved by this document.

## Policy reconciliation

| Topic | Existing ambiguity | Proposed implementation boundary |
| --- | --- | --- |
| Daily loss | 2% warning versus governance pause | Pause new exposure at 2% session loss; hard halt at 5%; flow-adjusted opening equity |
| Exposure | Per-order cash sizing versus NAV limits | 10% instrument, 25% sector, maximum gross 1.0, pending-order worst cases included |
| ETFs | Broad funds have multiple sectors | Approved dated look-through weights; unavailable/stale mapping blocks new risk |
| VaR | Missing history appeared as zero | Unavailable until at least 60 aligned daily observations; one-day 95% estimate |
| Liquidity | Missing history/volume | Fail closed for increased exposure; explicit approved volume/slippage policy |
| Paper evidence | Historical defaults 1/3/5 sessions | 5 complete clean sessions for dependable paper; 20 representative sessions for live pilot |
| Stop actions | Halt versus liquidation | Cancel owned orders, account late fills, report uncertainty; liquidation requires separate policy |

All numeric settings above are proposed software policy, requiring owner acceptance
before capital activation. Preserve Compliance veto, approval expiration, signing,
account boundaries and coverage. A risk-reducing route cannot cross zero or increase
absolute exposure, bypass Compliance, or conceal outstanding opposing orders.

## Data and operational prerequisites

New exposure requires valid event/availability/receive times, finite positive quotes,
approved data provenance and current price/history/liquidity/sector evidence. News and
fundamental strategies require individually available research records. Missing features
mean non-participation; no synthetic sentiment or current vendor snapshot counts as
historical evidence. Fixture feeds prove software behavior only.

Broker qualification remains blocked until actual account/mode, ownership prefix,
entitlements, caps, cleanup/position-retention policy and authorized credentials are
available. This implementation does not read or modify existing `.env` or trading storage.
Actual live enablement and orders remain outside this authorization.

## Later milestones

Shorting, options, futures, FX, crypto, additional currencies/venues, multi-account customer
service and autonomous upward strategy promotion remain separate explicit milestones.
Software completion, strategy acceptance, dependable paper and supervised live pilot
qualification are separate statuses. The initial equities release does not complete the
original multi-asset executive specification.
