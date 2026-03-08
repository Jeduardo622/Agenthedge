"""Break-glass override controls with TTL and auditable reason."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol

from .postgres import ensure_postgres_schema, postgres_connection


class BreakGlassError(ValueError):
    """Raised when break-glass requests are invalid."""


class BreakGlassStore(Protocol):
    def activate(
        self,
        *,
        control_name: str,
        reason: str,
        created_by: str,
        ttl_seconds: int,
    ) -> str: ...

    def revoke(self, *, override_id: str, revoked_by: str) -> bool: ...

    def is_active(self, control_name: str) -> bool: ...

    def active_overrides(self) -> list[Mapping[str, object]]: ...


class NullBreakGlassStore:
    def activate(
        self,
        *,
        control_name: str,
        reason: str,
        created_by: str,
        ttl_seconds: int,
    ) -> str:
        raise BreakGlassError("break-glass is disabled")

    def revoke(self, *, override_id: str, revoked_by: str) -> bool:
        return False

    def is_active(self, control_name: str) -> bool:
        return False

    def active_overrides(self) -> list[Mapping[str, object]]:
        return []


@dataclass(frozen=True)
class PostgresBreakGlassStore:
    dsn: str
    max_ttl_seconds: int = 86_400

    def __post_init__(self) -> None:
        ensure_postgres_schema(self.dsn)

    def activate(
        self,
        *,
        control_name: str,
        reason: str,
        created_by: str,
        ttl_seconds: int,
    ) -> str:
        control = control_name.strip()
        if not control:
            raise BreakGlassError("control_name is required")
        reason_text = reason.strip()
        if not reason_text:
            raise BreakGlassError("reason is required")
        actor = created_by.strip()
        if not actor:
            raise BreakGlassError("created_by is required")
        if ttl_seconds <= 0:
            raise BreakGlassError("ttl_seconds must be positive")
        if ttl_seconds > self.max_ttl_seconds:
            raise BreakGlassError(f"ttl_seconds exceeds max_ttl_seconds ({self.max_ttl_seconds})")
        override_id = str(uuid.uuid4())
        with postgres_connection(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ah_break_glass_overrides (
                        override_id,
                        control_name,
                        reason,
                        expires_at,
                        created_by,
                        created_at
                    ) VALUES (
                        %s, %s, %s, NOW() + (%s * INTERVAL '1 second'), %s, NOW()
                    )
                    """,
                    (
                        override_id,
                        control,
                        reason_text,
                        int(ttl_seconds),
                        actor,
                    ),
                )
        return override_id

    def revoke(self, *, override_id: str, revoked_by: str) -> bool:
        actor = revoked_by.strip()
        if not actor:
            raise BreakGlassError("revoked_by is required")
        with postgres_connection(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ah_break_glass_overrides
                    SET revoked_at = NOW()
                    WHERE override_id = %s
                      AND revoked_at IS NULL
                      AND expires_at > NOW()
                    """,
                    (override_id,),
                )
                return int(cur.rowcount) > 0

    def is_active(self, control_name: str) -> bool:
        with postgres_connection(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM ah_break_glass_overrides
                    WHERE control_name = %s
                      AND revoked_at IS NULL
                      AND expires_at > NOW()
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (control_name,),
                )
                return cur.fetchone() is not None

    def active_overrides(self) -> list[Mapping[str, object]]:
        with postgres_connection(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT override_id, control_name, reason, expires_at, created_by, created_at
                    FROM ah_break_glass_overrides
                    WHERE revoked_at IS NULL
                      AND expires_at > NOW()
                    ORDER BY expires_at ASC
                    """
                )
                rows = cur.fetchall()
        active: list[Mapping[str, object]] = []
        for row in rows:
            active.append(
                {
                    "override_id": str(row[0]),
                    "control_name": str(row[1]),
                    "reason": str(row[2]),
                    "expires_at": _to_iso(row[3]),
                    "created_by": str(row[4]),
                    "created_at": _to_iso(row[5]),
                }
            )
        return active


def _to_iso(raw: object) -> str:
    if isinstance(raw, datetime):
        if raw.tzinfo:
            return raw.astimezone(timezone.utc).isoformat()
        return raw.replace(tzinfo=timezone.utc).isoformat()
    return str(raw)
