# Explicit consolidated SIP runtime contract

SIP support is an opt-in software capability. It does not grant a market-data
subscription, update active configuration, qualify a release or start a worker.
The owner enables the required Alpaca entitlement outside this implementation.

An approved descriptor selects `schema_version: 3`, `provider: "alpaca_sip"`,
and `quote_policy.research_feed: "sip"`. It also supplies the same explicit
`provider_config`, adjacent `research_file` and `research_sha256` fields required
by the other runtime descriptors. Schema 1 Finnhub and schema 2 `alpaca_iex`
remain distinct contracts; changing a schema number alone cannot convert data.

The quote policy retains the approved ceilings:

```json
{
  "max_age_seconds": 5,
  "max_spread_fraction": "0.001",
  "research_feed": "sip"
}
```

Any stricter configured provider or risk-mark freshness remains in force. The
runtime requires actual raw price-history and liquidity records with source
`alpaca:sip`, including a qualified previous venue close. It rejects IEX records
under a SIP descriptor. Existing classification, ETF look-through, liquidity,
history and point-in-time availability requirements are unchanged. A current
download must retain its actual receipt time; it cannot create historical
availability or update an old source's as-of date.

`AlpacaSipProvider` requests authenticated, uncached latest quote and trade
observations using explicit `feed=sip`. It preserves both original timestamps,
uses `alpaca:sip` provenance, and rejects stale/future times, invalid prices,
crossed quotes and excessive spread. HTTP 403, missing entitlement and other
capture failures do not trigger an IEX, delayed-SIP, cached or daily-bar fallback.
The existing `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY` process variables
are reused; no new credential or billing operation is performed.

The same side-specific limit checks, uncached pre-submission capture, exact
captured-object validation and final authority/freshness checks apply to SIP.
The installed paper mandate accepts SIP only through the approved descriptor,
research hashes and release identity; account, symbols, capital, quantities,
reservations and halt restrictions are unchanged.

The separate qualification collector supports explicit SIP policy selection and
signs `feed: "sip"` in its observation details. It retains the four fixed GETs,
explicit clock budgets and no-activation boundary. For SIP descriptors the CLI
also validates the research path, hash and raw SIP price/liquidity provenance
before network access and checks that those bytes remain unchanged afterward.
This does not attest complete historical coverage or metadata freshness: those
remain independently reviewed runtime/data-manifest acceptance requirements.

Before activation, collect genuinely current SIP data and research, validate the
new exact code/configuration/policy/strategy/data identity, and obtain the actual
reviewed G0–G2 dossier plus current passing signed preflight. Prior IEX data or
software proof must retain its original feed and release attribution. No active
trust or evidence file is provisioned by loading this contract.

Alpaca distinguishes its single-exchange IEX offering from consolidated SIP in
its [Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq).
