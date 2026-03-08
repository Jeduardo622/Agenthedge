"""Postgres-backed audit sink with hash-chain guarantees."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from infra.postgres import advisory_lock_key, ensure_postgres_schema, postgres_connection

from .sink import JsonlAuditSink, _hash_record, _resolve_context_ref, _serialize_record


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        raise TypeError("boolean is not a valid integer value in this context")
    if isinstance(value, (int, float, str)):
        return int(value)
    raise TypeError(f"cannot coerce {type(value).__name__} to int")


class PostgresAuditSink:
    def __init__(self, dsn: str, *, mirror_path: str | Path | None = None) -> None:
        self._dsn = dsn
        self._mirror = JsonlAuditSink(mirror_path) if mirror_path else None
        self._path = Path(mirror_path) if mirror_path else None
        self._lock_key = advisory_lock_key("ah_audit_hash_chain")
        ensure_postgres_schema(dsn)

    def __call__(
        self,
        action: str,
        payload: Mapping[str, object] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        payload_dict = dict(payload or {})
        metadata_dict = dict(metadata or {})
        context_ref = _resolve_context_ref(payload_dict)
        approvals = (
            payload_dict.get("approvals")
            if isinstance(payload_dict.get("approvals"), dict)
            else None
        )
        inputs = (
            payload_dict.get("data_metadata")
            if isinstance(payload_dict.get("data_metadata"), dict)
            else None
        )
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_key,))
                cur.execute("SELECT hash FROM ah_audit_events ORDER BY sequence_id DESC LIMIT 1")
                row = cur.fetchone()
                last_hash = str(row[0]) if row else None
                record: MutableMapping[str, object] = {
                    "event_id": str(uuid.uuid4()),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "agent_id": metadata_dict.get("agent_id"),
                    "run_id": metadata_dict.get("run_id"),
                    "environment": metadata_dict.get("environment"),
                    "event_type": action,
                    "context_ref": context_ref,
                    "inputs": inputs,
                    "approvals": approvals,
                    "action": action,
                    "payload": payload_dict,
                    "prev_hash": last_hash,
                }
                record_hash = _hash_record(record)
                record["hash"] = record_hash
                cur.execute(
                    """
                    INSERT INTO ah_audit_events (
                        event_id, event_timestamp, event_type, context_ref,
                        payload_json, metadata_json, prev_hash, hash
                    ) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
                    """,
                    (
                        record["event_id"],
                        record["timestamp"],
                        action,
                        context_ref,
                        _serialize_record(payload_dict),
                        _serialize_record(metadata_dict),
                        last_hash,
                        record_hash,
                    ),
                )
        if self._mirror:
            self._mirror(action, payload_dict, metadata_dict)

    @property
    def path(self) -> Path | None:
        return self._path


def fetch_audit_event_count(dsn: str) -> int:
    ensure_postgres_schema(dsn)
    with postgres_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ah_audit_events")
            row = cur.fetchone()
            return _as_int(row[0]) if row else 0
