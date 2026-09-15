# Reduce-only stop verification

The automatic stop producer limits its whole-share proposal to the explicit
`ReductionPolicy` cap, rounding the proposed quantity down. It never rounds or
deletes journal holdings. A cap smaller than one share emits the stop alert but
does not submit an unsupported fractional order through the whole-share route.

The maintained PostgreSQL pipeline test runs actual Risk, Compliance, Director
and Execution agents on the account-scoped durable bus. A restricted symbol gets
no provider submission; after the veto is removed, the approved bounded stop
crosses all three approvals and persists UNKNOWN before the synthetic broker
call. After a partial fill, replay and Risk producer restart with changed price
and holdings do not submit a second order for the same durable position episode.
The test uses synthetic broker observations, never real orders or observed-market
qualification.

The first full pipeline probe found a concrete sizing incompatibility: a 25% cap
on ten shares proposed 2.5 shares, which the existing whole-share Execution guard
rejected. Rounding only the proposed stop down to two fixes that path while
preserving the cap, Compliance veto, approval expiry and atomic position checks.

Remaining acceptance includes independent review and integration with the actual
operator worker. A broker-supported, separately authorized fractional residual
reduction route is still required for the specification's corporate-action
residual workflow. The whole-share automatic stop does not establish that route.
