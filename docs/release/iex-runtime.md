# Explicit IEX runtime contract

This contract is software capability, not provider entitlement or paper qualification.
The existing schema 1 Finnhub descriptor remains supported. An approved paper
mandate may select schema 2 with `provider: "alpaca_iex"` and this additional field:

```json
"quote_policy": {
  "max_age_seconds": 5,
  "max_spread_fraction": "0.001",
  "research_feed": "iex"
}
```

The other required descriptor keys remain `provider_config`, `research_file`, and
`research_sha256`. Public provider configuration excludes credentials. The existing
`ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY` are supplied only in the isolated
worker environment, without changing any other account credentials.

Both latest trade and latest bid/ask are fetched from authenticated Alpaca stock
data endpoints with explicit `feed=iex`, no cache, redirects, or fallback. Every
observation must be no older than the stricter of the quote policy and existing
provider freshness. Future times, nonpositive or nonfinite prices, crossed quotes,
or spread above the approved fraction fail closed. The snapshot event timestamp is
the actual trade timestamp; a separate quote timestamp preserves bid/ask provenance.
Both timestamps must pass freshness checks. The checksum binds both response objects.

The immutable research bundle must use raw price bars and source `alpaca:iex` for
price and liquidity records. This prevents silent substitution of SIP previous
closes or consolidated volume. The prior close comes from the actual previous
venue session, retaining its record identity; existing visible split adjustment
is applied by the strategy reference-price path. Existing sourced risk contracts
retain aligned-history, classification, liquidity freshness, ETF look-through
weight/age and exposure checks. Do not relabel observation availability as its
historical session time when constructing a bundle from a current download.

`execution_limit` selects captured ask for buys and bid for sells. The submission
boundary calls `revalidate_order` for a new uncached capture and rejects a buy
above the new ask or sell below the new bid. It never changes quantity or price.
Because capture consumes time, submission must subsequently recheck independent
release, lease, reservation and risk-approval expiry before broker submission.
After those blocking checks, `validate_execution_snapshot` checks the exact captured
object, local provider bindings and both timestamps without file or network I/O.
Descriptor/research hashes remain checked by capture and installed release authorization.

Actual account identity, broker baseline, entitlement, observed input coverage,
ETF source validation and independent signature remain operational gates.
