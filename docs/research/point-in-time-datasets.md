# Point-in-time backtest datasets

Qualified local backtests accept `--dataset-bundle PATH`. A bundle is a JSON object with
exactly `manifest` and `records`. The manifest identifies the source, license, creation
time, XNYS calendar, coverage, universe policy, price convention, limitations, and the
SHA-256 checksum of the complete record list.

Every record carries a stable `record_id`, `kind`, symbol, event and availability times,
source, revision, and lowercase SHA-256 checksum. Price records also carry the XNYS
`session`; their event time must equal that session's close. Availability controls what a
decision may see. Later revisions replace an earlier version only after their own
availability time. The bundle creation time describes the release and never substitutes
for record availability.

Supported price conventions are `raw`, `split_adjusted`, and
`total_return_adjusted`. Corporate actions may accompany only conventions that do not
already contain that adjustment. Splits apply at the later of effective and availability
time. USD cash dividends require an explicit entitlement cutoff and apply at the later of
payable and availability time using the exact position at that cutoff. Fractional split
positions remain exact; the importer does not invent rounding or cash in lieu.

Point-in-time universe records prevent current constituents from leaking into historical
runs. Static universes must state their survivorship limitation. Fundamentals and news
remain individual provenance-bearing observations. Missing or future observations do not
participate.

## Qualified risk inputs

A bundle may add a `risk_contract` to its manifest. The complete contract contains an
explicit risk policy mapping, positive freshness limits for marks, classifications, and
liquidity, a positive decision-artifact lifetime, and
`reference_price_convention: split_adjusted`. Price records then require a positive
`reference_close`; strategy return comparisons and return history use that split-adjusted
series. Strategy sizing, proposals, risk marks, fills, and portfolio valuation use the raw
`close`, so share quantities and executable prices stay in the same units while a supplied
split action affects holdings once and does not create a false price return.

Risk-qualified records use the same event, availability, revision, source, and checksum
fields as other records. `risk_classification` records declare `asset_type` (`equity` or
`etf`) and an equity sector. `risk_liquidity` records declare positive
`average_daily_volume`. `etf_sector_map` records declare an `as_of` date and sector
weights summing to one. A missing contract or missing visible input leaves risk
unavailable and therefore denies new exposure. Future revisions remain invisible until
their recorded availability time.

The output records the resolved policy hash and labels the bundle policy
`proposed_unapproved`. Loading a policy from a dataset does not attest owner approval.

Bundles must be supplied under the user's own data license. This interface does not grant
provider entitlements. Synthetic fixtures must identify themselves as synthetic and do
not establish real-market or promotion evidence. The unqualified YFinance path lacks this
record-level provenance and remains outside qualified point-in-time evidence.
