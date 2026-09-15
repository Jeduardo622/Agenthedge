# Fractional residual reduction

The whole-share rule remains the only route for new-risk submissions. A separate,
owner-supplied `FractionalResidualPolicy` may authorize closing one positive holding
below one share when a corporate action left that exact quantity in the canonical
ledger.

The policy binds account, paper/live mode, maximum residual, expiry, capability age,
and the Alpaca Trading API fractional-quantity route in its content hash. Risk obtains
fresh account, asset `fractionable`, and exact position evidence from the broker. Risk,
Compliance, and Director preserve that evidence through their ordinary approval path.
Execution reads the capability again before admission and immediately before sending,
while the existing `ReductionPolicy` journal checks still prevent a cross-zero sale or
conflict with working sell reservations. A missing, stale, changed, or wrong-namespace
capability fails closed.

The adapter uses the existing `POST /v2/orders` client-order-ID and unknown-submission
recovery boundary. Alpaca documents fractional `qty` for this endpoint and rejects an
asset that is not fractionable:
<https://docs.alpaca.markets/us/docs/fractional-trading>. The close-position endpoint
also describes fractional liquidation, but this route does not use it because it does
not provide the same explicit client-order identity contract:
<https://docs.alpaca.markets/us/reference/deleteopenposition-1>.

Tests use synthetic broker capability and PostgreSQL state only. They do not establish
that an actual account, asset, session, or entitlement supports fractional orders.
Owner policy acceptance and real paper-account qualification remain external release
evidence.
