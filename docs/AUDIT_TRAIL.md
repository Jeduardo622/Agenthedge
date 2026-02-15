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
| `agent_id` | Logical agent name (plus version hash if available). |
| `run_id` | Runtime correlation ID for the decision cycle. |
| `environment` | Runtime environment label (e.g., development, staging). |
| `event_type` | e.g., `proposal`, `risk_approval`, `compliance_veto`, `execution_fill`, `alert`. |
| `context_ref` | Decision/trade ID linking related events. |
| `inputs` | Key input summaries + data lineage references. |
| `outputs` | Decisions, metrics, or status changes. |
| `tools_called` | List of tools/APIs invoked with parameters summary. |
| `approvals` | Sign-offs (risk/compliance) with status + rationale. |
| `prev_hash` | Hash of the previous event (hash chain). |
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
- `AuditAgent` now produces JSON reports under `storage/audit/reports/weekly_<ISO week>.json` and logs `audit_weekly_report` events. Latest metadata mirrored in `storage/audit/reports/index.json`.

## Traceability Requirements
- Each trade must be traceable from idea inception ➝ approvals ➝ execution ➝ performance outcome.
- Provide CLI/Notebook query helpers (e.g., `python scripts/query_audit.py --trade-id 123`) to reconstruct narratives.
- Integrate with observability dashboard for drill-down views.

## Compliance Alignment
- Meets MiFID II and SEC Rule 613 expectations for algorithmic trading records (orders, quotes, decisions, timestamps).
- Supports SOC2 evidence gathering by proving change management + incident response actions were executed.

## Implementation Notes (Phase 2)
- Default sink: `storage/audit/runtime_events.jsonl` via `audit.JsonlAuditSink`.
- Serialization schema (current):
  ```json
  {
    "event_id": "uuid",
    "timestamp": "2026-01-30T12:00:00Z",
    "agent_id": "risk",
    "run_id": "20260130T120000Z",
    "environment": "development",
    "event_type": "risk_approval",
    "context_ref": "decision-uuid",
    "approvals": {"risk": {"status": "approved", "timestamp": "..."}},
    "action": "risk_approval",
    "payload": { "... agent-defined fields ..." },
    "prev_hash": "sha256",
    "hash": "sha256"
  }
  ```
- Agents call `BaseAgent.audit(action, payload)` and the runtime injects metadata through `AgentContext`.
- Hashes are chained per record to enable tamper detection; rotate files via ops process (weekly by default).
