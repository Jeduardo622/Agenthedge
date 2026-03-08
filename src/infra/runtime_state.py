"""Durable runtime/scheduler/provider state sinks."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Mapping, MutableMapping, Protocol

from .postgres import ensure_postgres_schema, postgres_connection


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        raise TypeError("boolean is not a valid integer value in this context")
    if isinstance(value, (int, float, str)):
        return int(value)
    raise TypeError(f"cannot coerce {type(value).__name__} to int")


class RuntimeStateSink(Protocol):
    def mark_started(self) -> None: ...

    def heartbeat(self, *, status: str) -> None: ...

    def record_incident(self, event_type: str, payload: Mapping[str, object]) -> None: ...

    def record_scheduler_run(
        self,
        *,
        job_name: str,
        status: str,
        details: Mapping[str, object],
    ) -> None: ...

    def record_provider_health(self, payload: Mapping[str, Mapping[str, object]]) -> None: ...

    def acquire_lease(self, *, runtime_name: str, lease_seconds: int) -> tuple[bool, int]: ...

    def renew_lease(
        self,
        *,
        runtime_name: str,
        fence_token: int,
        lease_seconds: int,
    ) -> bool: ...

    def release_lease(self, *, runtime_name: str, fence_token: int) -> None: ...

    def load_checkpoint(self, *, runtime_name: str) -> MutableMapping[str, object] | None: ...

    def save_checkpoint(
        self,
        *,
        runtime_name: str,
        fence_token: int | None,
        tick_count: int,
        bus_checkpoint: int,
        kill_switch_reason: str | None,
        kill_switch_trigger: str | None,
        payload: Mapping[str, object] | None = None,
    ) -> None: ...


class NullRuntimeStateSink:
    def mark_started(self) -> None:
        return

    def heartbeat(self, *, status: str) -> None:
        return

    def record_incident(self, event_type: str, payload: Mapping[str, object]) -> None:
        return

    def record_scheduler_run(
        self,
        *,
        job_name: str,
        status: str,
        details: Mapping[str, object],
    ) -> None:
        return

    def record_provider_health(self, payload: Mapping[str, Mapping[str, object]]) -> None:
        return

    def acquire_lease(self, *, runtime_name: str, lease_seconds: int) -> tuple[bool, int]:
        return (True, 0)

    def renew_lease(
        self,
        *,
        runtime_name: str,
        fence_token: int,
        lease_seconds: int,
    ) -> bool:
        return True

    def release_lease(self, *, runtime_name: str, fence_token: int) -> None:
        return

    def load_checkpoint(self, *, runtime_name: str) -> MutableMapping[str, object] | None:
        return None

    def save_checkpoint(
        self,
        *,
        runtime_name: str,
        fence_token: int | None,
        tick_count: int,
        bus_checkpoint: int,
        kill_switch_reason: str | None,
        kill_switch_trigger: str | None,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        return


class PostgresRuntimeStateSink:
    def __init__(
        self,
        dsn: str,
        *,
        instance_id: str,
        profile: str,
        backend: str,
    ) -> None:
        self._dsn = dsn
        self._instance_id = instance_id
        self._profile = profile
        self._backend = backend
        ensure_postgres_schema(dsn)

    def mark_started(self) -> None:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ah_runtime_instances (
                        instance_id,
                        profile,
                        backend,
                        status,
                        started_at,
                        last_heartbeat,
                        updated_at
                    ) VALUES (%s, %s, %s, %s, NOW(), NOW(), NOW())
                    ON CONFLICT (instance_id) DO UPDATE
                    SET profile = EXCLUDED.profile,
                        backend = EXCLUDED.backend,
                        status = EXCLUDED.status,
                        last_heartbeat = NOW(),
                        updated_at = NOW()
                    """,
                    (self._instance_id, self._profile, self._backend, "running"),
                )

    def heartbeat(self, *, status: str) -> None:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ah_runtime_instances
                    SET status = %s, last_heartbeat = NOW(), updated_at = NOW()
                    WHERE instance_id = %s
                    """,
                    (status, self._instance_id),
                )

    def record_incident(self, event_type: str, payload: Mapping[str, object]) -> None:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ah_runtime_incidents (instance_id, event_type, payload_json)
                    VALUES (%s, %s, %s::jsonb)
                    """,
                    (self._instance_id, event_type, json.dumps(dict(payload), default=str)),
                )

    def record_scheduler_run(
        self,
        *,
        job_name: str,
        status: str,
        details: Mapping[str, object],
    ) -> None:
        run_id = str(uuid.uuid4())
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ah_scheduler_runs (
                        run_id, job_name, status, details_json, instance_id, created_at
                    ) VALUES (%s, %s, %s, %s::jsonb, %s, NOW())
                    """,
                    (
                        run_id,
                        job_name,
                        status,
                        json.dumps(dict(details), default=str),
                        self._instance_id,
                    ),
                )

    def record_provider_health(self, payload: Mapping[str, Mapping[str, object]]) -> None:
        observed_at = datetime.now(timezone.utc).isoformat()
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                for provider, details in payload.items():
                    available = bool(details.get("available"))
                    snapshot = dict(details)
                    snapshot["observed_at"] = observed_at
                    cur.execute(
                        """
                        INSERT INTO ah_provider_health_snapshots (
                            instance_id, provider, available, payload_json, observed_at
                        ) VALUES (%s, %s, %s, %s::jsonb, NOW())
                        """,
                        (
                            self._instance_id,
                            provider,
                            available,
                            json.dumps(snapshot, default=str),
                        ),
                    )

    def acquire_lease(self, *, runtime_name: str, lease_seconds: int) -> tuple[bool, int]:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ah_runtime_leases (
                        runtime_name,
                        owner_instance_id,
                        fence_token,
                        lease_expires_at,
                        updated_at
                    ) VALUES (%s, %s, 1, NOW() + (%s * INTERVAL '1 second'), NOW())
                    ON CONFLICT (runtime_name) DO UPDATE
                    SET owner_instance_id = CASE
                            WHEN ah_runtime_leases.lease_expires_at <= NOW()
                              OR ah_runtime_leases.owner_instance_id = EXCLUDED.owner_instance_id
                            THEN EXCLUDED.owner_instance_id
                            ELSE ah_runtime_leases.owner_instance_id
                        END,
                        fence_token = CASE
                            WHEN ah_runtime_leases.lease_expires_at <= NOW()
                              OR ah_runtime_leases.owner_instance_id = EXCLUDED.owner_instance_id
                            THEN ah_runtime_leases.fence_token + 1
                            ELSE ah_runtime_leases.fence_token
                        END,
                        lease_expires_at = CASE
                            WHEN ah_runtime_leases.lease_expires_at <= NOW()
                              OR ah_runtime_leases.owner_instance_id = EXCLUDED.owner_instance_id
                            THEN NOW() + (%s * INTERVAL '1 second')
                            ELSE ah_runtime_leases.lease_expires_at
                        END,
                        updated_at = NOW()
                    RETURNING owner_instance_id, fence_token
                    """,
                    (
                        runtime_name,
                        self._instance_id,
                        int(lease_seconds),
                        int(lease_seconds),
                    ),
                )
                row = cur.fetchone()
                if not row:
                    return (False, 0)
                owner = str(row[0])
                token = _as_int(row[1])
                return (owner == self._instance_id, token)

    def renew_lease(
        self,
        *,
        runtime_name: str,
        fence_token: int,
        lease_seconds: int,
    ) -> bool:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ah_runtime_leases
                    SET lease_expires_at = NOW() + (%s * INTERVAL '1 second'),
                        updated_at = NOW()
                    WHERE runtime_name = %s
                      AND owner_instance_id = %s
                      AND fence_token = %s
                      AND lease_expires_at > NOW()
                    """,
                    (
                        int(lease_seconds),
                        runtime_name,
                        self._instance_id,
                        int(fence_token),
                    ),
                )
                return int(cur.rowcount) > 0

    def release_lease(self, *, runtime_name: str, fence_token: int) -> None:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM ah_runtime_leases
                    WHERE runtime_name = %s
                      AND owner_instance_id = %s
                      AND fence_token = %s
                    """,
                    (runtime_name, self._instance_id, int(fence_token)),
                )

    def load_checkpoint(self, *, runtime_name: str) -> MutableMapping[str, object] | None:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        instance_id,
                        fence_token,
                        tick_count,
                        bus_checkpoint,
                        kill_switch_reason,
                        kill_switch_trigger,
                        payload_json
                    FROM ah_runtime_checkpoints
                    WHERE runtime_name = %s
                    """,
                    (runtime_name,),
                )
                row = cur.fetchone()
        if not row:
            return None
        payload_raw = row[6]
        payload = payload_raw if isinstance(payload_raw, Mapping) else {}
        return {
            "instance_id": str(row[0]),
            "fence_token": _as_int(row[1]) if row[1] is not None else None,
            "tick_count": _as_int(row[2]),
            "bus_checkpoint": _as_int(row[3]),
            "kill_switch_reason": str(row[4]) if row[4] else None,
            "kill_switch_trigger": str(row[5]) if row[5] else None,
            "payload": dict(payload),
        }

    def save_checkpoint(
        self,
        *,
        runtime_name: str,
        fence_token: int | None,
        tick_count: int,
        bus_checkpoint: int,
        kill_switch_reason: str | None,
        kill_switch_trigger: str | None,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ah_runtime_checkpoints (
                        runtime_name,
                        instance_id,
                        fence_token,
                        tick_count,
                        bus_checkpoint,
                        kill_switch_reason,
                        kill_switch_trigger,
                        payload_json,
                        updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
                    ON CONFLICT (runtime_name) DO UPDATE
                    SET instance_id = EXCLUDED.instance_id,
                        fence_token = EXCLUDED.fence_token,
                        tick_count = EXCLUDED.tick_count,
                        bus_checkpoint = EXCLUDED.bus_checkpoint,
                        kill_switch_reason = EXCLUDED.kill_switch_reason,
                        kill_switch_trigger = EXCLUDED.kill_switch_trigger,
                        payload_json = EXCLUDED.payload_json,
                        updated_at = NOW()
                    """,
                    (
                        runtime_name,
                        self._instance_id,
                        int(fence_token) if fence_token is not None else None,
                        int(tick_count),
                        int(bus_checkpoint),
                        kill_switch_reason,
                        kill_switch_trigger,
                        json.dumps(dict(payload or {}), default=str),
                    ),
                )
