# Explicit SIP data integration implementation plan

**Goal:** Add an explicitly selected consolidated SIP contract while retaining the approved safety ceilings and current IEX behavior. The owner selected this path on October 28, 2026 and will enable market-data entitlement separately.

**Architecture:** Add `data.sip` provider/policy types, schema 3 `alpaca_sip` research descriptors, and explicit SIP selection in read-only preflight and installed mandate validation. Existing schema 1 Finnhub and schema 2 IEX remain supported. No active operational configuration, trust, evidence, credentials or worker is switched by this change.

**Classification:** Protected but contained market-data/runtime-boundary implementation. No route-task helper exists. Affected production surfaces are `data/sip.py`, `ops/runtime_data.py`, `ops/paper_preflight.py`, `cli/paper_qualification_preflight.py`, and the provider admission check in `ops/artifacts.py`. Quant, order construction, risk policy, submission authority, database schema, deployment and CI policy are outside this slice.

## Constraints and acceptance

- Both actual quote and trade timestamps must remain within the stricter existing limit, never above 5 seconds; no timestamp rewriting, cache substitution, retry-until-green or IEX fallback on SIP failure.
- Preserve positive finite prices, bid/ask ordering and spread fraction at most 0.001. Preserve side-specific limit revalidation and the pure final captured-object/authority checks.
- New schema 3 requires provider `alpaca_sip`, policy `research_feed=sip`, raw price history and liquidity provenance `alpaca:sip`. No mixed IEX/SIP historical inputs.
- Preflight signs the actual selected feed, retains its four fixed GETs and explicit clock budgets, and holds on 403. SIP CLI also validates the descriptor's research path/hash/provenance without a worker or network capture before input validation.
- No new dependencies, environment variables, billing action or subscription purchase. Tests use synthetic credentials, clocks and transports only.
- Existing 69f664e software results and approved trust remain historical base evidence. Any new source/data identity requires its own checks and issuer acceptance; no old evidence is relabeled.

## Execution

1. Add failing provider, runtime, preflight, CLI and installed-mandate tests for explicit SIP selection, mixed-feed rejection, 403/no fallback, exact freshness boundaries and unchanged submission limits.
2. Implement the minimum provider and existing boundary changes. Reuse existing canonical market-data and signature formats.
3. Run focused safety regressions, static checks, package smoke and the isolated disposable-PostgreSQL suite. Obtain independent nonauthor review and fix findings within this boundary.
4. Prepare an unsigned G0–G2 dossier candidate with every outstanding acceptance explicitly held, linked source hashes, selected test proof, and a separate record of missing entitlement/current data/operational evidence. Do not sign or publish an active release dossier.

Real SIP availability, refreshed SIP-compatible research, current issuer metadata and a genuine passing signed preflight remain operational acceptance requirements after the owner enables access.
