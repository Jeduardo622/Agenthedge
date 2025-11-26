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
| Network & APIs | Restrict outbound calls to approved domains. Enforce TLS. Monitor API rate + failure anomalies. | Data agent maintains allowlist. |
| Code Integrity | Signed releases, checksum validation for agent prompts/configs. Pre-commit + CI security scans. | Leverage dependency scanning (pip-audit). |
| Runtime Monitoring | Heartbeat checks per agent, anomaly detection on behavior (e.g., unusual order frequency). | Alerts integrate with ops channels. |
| Kill Switches | Global manual kill (human), automated kill on compliance/risk/security triggers, per-agent disable toggles. | Execution agent cancels outstanding orders on trigger. |
| Logging & Forensics | Immutable logs with tamper-evident storage; replicate to cold storage daily. | Supports investigations and regulatory reporting. |
| Environment Hardening | Run agents in sandboxed containers/VMs, enforce OS patches, minimize installed software. | For local dev, use virtualenv and restricted OS account. |

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
