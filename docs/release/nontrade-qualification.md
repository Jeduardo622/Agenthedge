# Nontrade activity qualification

Agenthedge's existing retail Trading API reader uses `GET /v2/account/activities`.
Its nontrade `date` can represent occurrence or settlement, while request bounds
refer to creation time. Date-only records therefore remain raw and unresolved;
the reader does not manufacture a midnight economic time.

`normalize_v2_nontrade_activity` is a pure boundary for the newer Activity Events
V2 shape documented for the Broker API activity stream/history. It does not fetch
that product and does not imply that a retail Trading API account is entitled to
or returns these fields. A separate authenticated reader, cursor, and product
access qualification are required before these events can reach reconciliation.

The initial qualified subset requires a matching account, paper/live namespace,
executed status, USD currency, exact `executed_at`, business/source-system `at`, stable
`ref_id`, canonical ULID publication `event_id`, and complete type-specific decimals.
Publication time is decoded from the ULID timestamp as documented by Alpaca. It accepts
cash dividends (`DIV/CDIV`), margin interest (`INT/MGN`), documented unlinked equity
fees (`REG`, `TAF`, `CAT`, `ADR`, `BSWP`, `NRV`, `NRC`), and cashless equity forward
or reverse splits. The full provider record is hashed. `ref_id` is the canonical
economic identity; business time, publication identity, and decoded publication time
remain separately visible.

Standalone fee references use the stable provider activity reference solely for
economic deduplication. They do not assert a trade owner. Legacy/date-only data,
corrections (`previous_id`), tax and dividend adjustments, return of capital,
transfers, journals, ACATS, mergers, spin-offs, name/symbol changes, reorganizations,
options (including `ORF` and `OCOM` fees), local-currency fees, non-margin interest,
fractional cash-in-lieu, non-USD events, and incomplete split rates remain
unqualified.

Official contracts checked:

- [Trading API account activities](https://docs.alpaca.markets/us/docs/account-activities)
- [Trading API activity query creation-time semantics](https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1)
- [Broker API Activity Events V2 fields and migration notes](https://docs.alpaca.markets/us/docs/activity-sse)
