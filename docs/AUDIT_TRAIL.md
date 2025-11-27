# Audit Trail Specification

Inspired by `ExecSpec.md` (auditability) and `Designing an Autonomous Multi-Agent Financial Trading System.pdf` (logging & observability).

## Objectives
- Capture complete, tamper-evident records of every decision cycle.
- Support regulatory audits, incident investigations, and model introspection.

## Data Model
| Field | Description |
| --- | --- |
| `event_id` | UUID for the log entry. |
| `timestamp` | ISO8601 in UTC. |
| `agent_id` | Logical agent name + version hash. |
| `event_type` | e.g., `proposal`, `risk_approval`, `compliance_veto`, `execution_fill`, `alert`. |
| `context_ref` | Decision/trade ID linking related events. |
| `inputs` | Key input summaries + data lineage references. |
| `outputs` | Decisions, metrics, or status changes. |
| `tools_called` | List of tools/APIs invoked with parameters summary. |
| `approvals` | Sign-offs (risk/compliance) with status + rationale. |
| `hash` | Cryptographic hash for tamper detection. |

Store records in append-only log (JSONL) plus replicated datastore (S3, database). Apply daily hashing chain (Merkle tree or sequential hash) for integrity.

## Retention & Access
- **Retention:** 7 years default (configurable per jurisdiction). Hot storage 90 days, warm storage 1 year, cold archive remainder.
- **Access Control:** Read-only for most agents; write-only for producers. Compliance agent and human auditors granted query capability via tooling.
- **Redaction:** If personal data ever introduced (future modules), apply tokenization/anonymization before persistence.

## Reporting
- Daily summary auto-generated highlighting:
  - Number of trades approved/blocked.
  - Risk breaches and actions taken.
  - Compliance alerts and resolutions.
  - Execution anomalies (slippage > tolerance).
- Weekly digest packaged for oversight board; archived with version tag.

## Traceability Requirements
- Each trade must be traceable from idea inception ➝ approvals ➝ execution ➝ performance outcome.
- Provide CLI/Notebook query helpers (e.g., `python scripts/query_audit.py --trade-id 123`) to reconstruct narratives.
- Integrate with observability dashboard for drill-down views.

## Compliance Alignment
- Meets MiFID II and SEC Rule 613 expectations for algorithmic trading records (orders, quotes, decisions, timestamps).
- Supports SOC2 evidence gathering by proving change management + incident response actions were executed.

## Implementation Notes (Phase 1)
- Default sink: `storage/audit/runtime_events.jsonl` via `audit.JsonlAuditSink`.
- Serialization schema:
  ```json
  {
    "timestamp": "2025-01-01T12:00:00Z",
    "action": "execution_fill",
    "payload": { "... agent-defined fields ..." }
  }
  ```
- Agents call `BaseAgent.audit(action, payload)` and the runtime injects the sink through `AgentContext`.
- Files rotate via ops process (weekly by default); include in backup cadence.
