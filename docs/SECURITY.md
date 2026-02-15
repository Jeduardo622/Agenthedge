# Security Policy

Combines requirements from `ExecSpec.md` (security & kill switches) and the Technical Implementation Plan.

## Goals
- Protect credentials, infrastructure, and trading authority from misuse.
- Detect anomalies rapidly and enforce layered kill switches.
- Limit blast radius of any compromised agent.

## Controls
| Area | Control | Notes |
| --- | --- | --- |
| Identity & Access | Principle of least privilege per agent; trade-only broker keys without withdrawal rights. | Director cannot execute trades; Execution cannot change strategy. |
| Secrets Management | Store API keys in environment-specific vault or encrypted files. Rotate quarterly or after suspected exposure. | Never log raw secrets; use aliases. |
| Network & APIs | Restrict outbound calls to approved domains. Enforce TLS. Monitor API rate + failure anomalies. | Enforced via `NETWORK_ALLOWLIST_*` policy on provider + webhook HTTP paths. |
| Code Integrity | Signed releases, checksum validation for agent prompts/configs. Pre-commit + CI security scans. | Leverage dependency scanning (pip-audit). |
| Runtime Monitoring | Heartbeat checks per agent, anomaly detection on behavior (e.g., unusual order frequency). | Heartbeat timeout monitor and anomaly detector are active in runtime with alert + kill escalation. |
| Kill Switches | Global manual kill (human), automated kill on compliance/risk/security triggers, per-agent disable toggles. | Runtime halts ticks; Execution blocks new fills after trigger (paper engine has no outstanding order queue). |
| Message Bus Controls | Restrict publish topics by agent role using runtime ACLs. | Enforced by default outside development (`ENVIRONMENT!=development`) or explicitly via `BUS_ACL_ENFORCE`. |
| Logging & Forensics | Immutable logs with tamper-evident storage; replicate to cold storage daily. | Supports investigations and regulatory reporting. |
| Environment Hardening | Run agents in sandboxed containers/VMs, enforce OS patches, minimize installed software. | For local dev, use virtualenv and restricted OS account. |
| Adaptive Strategy Controls | `strategy.feedback` channel is authenticated; performance tracker weights (`storage/strategy_state/performance.json`) and backtest artifacts (`storage/backtests/`) are access-controlled. | Prevents malicious weight manipulation and ensures only approved backtest outputs gate live deployment. |

## Incident Response
1. Detect anomaly (security monitor, risk/compliance trigger, manual report).
2. Activate relevant kill switch (global or agent-specific).
3. Isolate affected components (revoke keys, stop services).
4. Preserve evidence (logs, memory snapshots).
5. Conduct postmortem with RCA, remediation plan, and update to `CHANGELOG.md` + `AUDIT_TRAIL`.

## Third-Party Dependencies
- Maintain SBOM (software bill of materials) for pip dependencies.
- Pin versions in `requirements.txt` / Poetry and monitor CVEs.
- Validate brokers/data vendors for security posture before integration.

## Testing & Drills
- Quarterly tabletop exercises for kill-switch activation and credential rotations.
- Automated tests verifying that unauthorized trades are blocked when keys are scoped correctly.
- Chaos-style simulations (disable data source, inject fake latency) to confirm resilience.
- Strategy feedback drills: simulate repeated risk/compliance breaches to confirm the Strategy Council down-ranks or disables offending strategies automatically.

## Control Status (Implementation Snapshot)
- **Implemented now:** tamper-evident audit chain, kill-switch propagation, execution approval-chain checks, replay protection, runtime circuit breaker, and ACL topic enforcement outside development.
- **Planned hardening:** advanced policy tuning for allowlist/anomaly thresholds and staged rollout in production environments.

## Secret Rotation Log (Phase 4 Launch)
| Secret | Scope | Last Rotated | Next Rotation | Owner | Notes |
| --- | --- | --- | --- | --- | --- |
| Alpha Vantage / Finnhub / NewsAPI | Market + sentiment data (read-only) | 2025-11-29 | 2026-02-28 | Data Ops | Keys stored in local `.env`, vaulted copy in Azure Key Vault `kv-agenthedge-dev`. |
| FRED API | Macro data (read-only) | 2025-11-29 | 2026-05-31 | Macro Ops | No PII, rotation aligned with quarterly macro review. |
| Alpaca Paper Trading | Trade-only paper creds | 2025-11-29 | 2026-02-28 | Trading Ops | Withdrawal rights disabled per broker console, alerts configured on credential use. |
| Alert Webhook Token | Observability notifications | 2025-11-20 | 2025-12-31 | SRE | Token stored in `.env`, mirrored into ops secret manager for CI. |
