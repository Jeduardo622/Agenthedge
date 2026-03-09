"""Postgres-backed durable message bus."""

from __future__ import annotations

import fnmatch
import json
import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Mapping, MutableMapping, Sequence

from infra.postgres import ensure_postgres_schema, postgres_connection

from .messaging import Envelope, Message, MessageBus, MessageHandler, Payload, Subscription


class PostgresMessageBus(MessageBus):
    def __init__(
        self,
        dsn: str,
        *,
        instance_id: str | None = None,
        max_history: int = 512,
        poll_interval_seconds: float = 0.1,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self._dsn = dsn
        self._instance_id = instance_id or str(uuid.uuid4())
        self._logger = logging.getLogger("agenthedge.postgres_message_bus")
        self._subs: Dict[str, Subscription] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._history: Deque[Envelope] = deque(maxlen=max_history)
        self._lock = threading.RLock()
        self._publish_acl: Dict[str, set[str]] = {}
        self._enforce_acl = False
        self._closed = False
        self._poll_interval_seconds = max(0.01, poll_interval_seconds)
        self._retry_delay_seconds = max(0.1, retry_delay_seconds)
        ensure_postgres_schema(dsn)

    def publish(
        self,
        topic: str,
        payload: Payload = None,
        *,
        publisher: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Envelope:
        if self._closed:
            raise RuntimeError("MessageBus is closed")
        if not self._is_allowed(topic, publisher):
            raise PermissionError(f"Publisher {publisher!r} not allowed for topic {topic!r}")
        payload_dict = dict(payload or {})
        metadata_dict = dict(metadata or {})
        metadata_dict["publisher"] = publisher
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ah_bus_events (
                        topic,
                        payload_json,
                        metadata_json,
                        publisher,
                        created_at
                    )
                    VALUES (%s, %s::jsonb, %s::jsonb, %s, NOW())
                    RETURNING event_id, created_at
                    """,
                    (
                        topic,
                        json.dumps(payload_dict, default=str),
                        json.dumps(metadata_dict, default=str),
                        publisher,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError("failed to insert bus event")
                event_id = _as_int(row[0])
                created_at = row[1]
                cur.execute(
                    """
                    SELECT subscription_id, topics_json
                    FROM ah_bus_subscriptions
                    WHERE active = TRUE
                    """
                )
                for sub_row in cur.fetchall():
                    sub_id = str(sub_row[0])
                    topics_json = sub_row[1]
                    topics = _decode_topics(topics_json)
                    if _matches(topic, topics):
                        cur.execute(
                            """
                            INSERT INTO ah_bus_deliveries (
                                subscription_id,
                                event_id,
                                status,
                                attempts,
                                next_attempt_at,
                                updated_at
                            ) VALUES (%s, %s, 'pending', 0, NOW(), NOW())
                            ON CONFLICT (subscription_id, event_id) DO NOTHING
                            """,
                            (str(sub_id), event_id),
                        )
        envelope = Envelope(
            id=str(event_id),
            message=Message(
                topic=topic,
                payload=payload_dict,
                created_at=_coerce_timestamp(created_at),
                metadata=metadata_dict,
            ),
        )
        with self._lock:
            self._history.append(envelope)
        return envelope

    def subscribe(
        self,
        handler: MessageHandler,
        *,
        topics: Sequence[str] | None = None,
        replay_last: int = 0,
        subscription_key: str | None = None,
    ) -> Subscription:
        if self._closed:
            raise RuntimeError("MessageBus is closed")
        subscription_id = subscription_key or str(uuid.uuid4())
        replaced_thread: threading.Thread | None = None
        with self._lock:
            replaced = self._subs.pop(subscription_id, None)
            if replaced:
                replaced.active = False
            replaced_thread = self._threads.pop(subscription_id, None)
        if replaced_thread:
            replaced_thread.join(timeout=2.0)
        subscription = Subscription(id=subscription_id, topics=topics, handler=handler)
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT cursor_event_id
                    FROM ah_bus_subscriptions
                    WHERE subscription_id = %s
                    """,
                    (subscription.id,),
                )
                existing_row = cur.fetchone()
                existing_cursor = _as_int(existing_row[0]) if existing_row else 0
                cur.execute(
                    """
                    INSERT INTO ah_bus_subscriptions (
                        subscription_id,
                        instance_id,
                        topics_json,
                        active,
                        cursor_event_id,
                        created_at,
                        updated_at
                    ) VALUES (%s, %s, %s::jsonb, TRUE, 0, NOW(), NOW())
                    ON CONFLICT (subscription_id) DO UPDATE
                    SET instance_id = EXCLUDED.instance_id,
                        topics_json = EXCLUDED.topics_json,
                        active = TRUE,
                        updated_at = NOW()
                    """,
                    (
                        subscription.id,
                        self._instance_id,
                        _encode_topics(topics),
                    ),
                )
                self._requeue_processing_deliveries(cur, subscription.id)
                if existing_row:
                    self._enqueue_backlog_deliveries(
                        cur,
                        subscription.id,
                        topics=topics,
                        after_event_id=existing_cursor,
                    )
                elif replay_last > 0:
                    self._enqueue_replay_deliveries(
                        cur,
                        subscription.id,
                        topics=topics,
                        replay_last=replay_last,
                    )
        with self._lock:
            self._subs[subscription.id] = subscription
            thread = threading.Thread(
                target=self._poll_subscription,
                args=(subscription,),
                name=f"PgBusSub-{subscription.id[:8]}",
                daemon=True,
            )
            self._threads[subscription.id] = thread
            thread.start()
        return subscription

    def unsubscribe(self, subscription_id: str) -> None:
        thread: threading.Thread | None = None
        with self._lock:
            subscription = self._subs.pop(subscription_id, None)
            if subscription:
                subscription.active = False
            thread = self._threads.pop(subscription_id, None)
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ah_bus_subscriptions
                    SET active = FALSE, updated_at = NOW()
                    WHERE subscription_id = %s
                      AND instance_id = %s
                    """,
                    (subscription_id, self._instance_id),
                )
        if thread:
            thread.join(timeout=2.0)

    def list_subscriptions(self) -> List[MutableMapping[str, object]]:
        with self._lock:
            return [
                {
                    "id": sub.id,
                    "topics": list(sub.topics) if sub.topics else ["*"],
                    "active": sub.active,
                }
                for sub in self._subs.values()
            ]

    def history(self, limit: int = 100) -> List[Envelope]:
        with self._lock:
            return list(self._history)[-limit:]

    def clear(self) -> None:
        with self._lock:
            sub_ids = list(self._subs.keys())
            for sub in self._subs.values():
                sub.active = False
            self._subs.clear()
            threads = list(self._threads.values())
            self._threads.clear()
            self._history.clear()
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                for sub_id in sub_ids:
                    cur.execute(
                        """
                        UPDATE ah_bus_subscriptions
                        SET active = FALSE
                        WHERE subscription_id = %s
                          AND instance_id = %s
                        """,
                        (sub_id, self._instance_id),
                    )
        for thread in threads:
            thread.join(timeout=2.0)

    def depth(self) -> int:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM ah_bus_events")
                row = cur.fetchone()
                return _as_int(row[0]) if row else 0

    def high_watermark(self) -> int:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COALESCE(MAX(event_id), 0) FROM ah_bus_events")
                row = cur.fetchone()
                return _as_int(row[0]) if row else 0

    def pending_deliveries(self) -> Dict[str, int]:
        sub_ids = list(self._subs.keys())
        if not sub_ids:
            return {}
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT subscription_id, COUNT(*)
                    FROM ah_bus_deliveries
                    WHERE subscription_id IN (
                        SELECT UNNEST(%s::text[])
                    )
                      AND status IN ('pending', 'processing', 'retry')
                    GROUP BY subscription_id
                    """,
                    (sub_ids,),
                )
                return {str(row[0]): _as_int(row[1]) for row in cur.fetchall()}

    def caught_up_checkpoint(self) -> int:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COALESCE(MAX(event_id), 0) FROM ah_bus_events")
                max_row = cur.fetchone()
                max_event_id = _as_int(max_row[0]) if max_row else 0
                cur.execute(
                    """
                    SELECT MIN(event_id)
                    FROM ah_bus_deliveries
                    WHERE subscription_id IN (
                        SELECT subscription_id
                        FROM ah_bus_subscriptions
                        WHERE active = TRUE
                          AND instance_id = %s
                    )
                      AND status IN ('pending', 'processing', 'retry')
                    """,
                    (self._instance_id,),
                )
                pending_row = cur.fetchone()
                if not pending_row or pending_row[0] is None:
                    return max_event_id
                min_pending = _as_int(pending_row[0])
                return max(0, min_pending - 1)

    def wait_until_caught_up(
        self,
        target_event_id: int,
        timeout_seconds: float,
        subscription_scope: Sequence[str] | None = None,
    ) -> bool:
        if target_event_id < 0:
            raise ValueError("target_event_id must be non-negative")
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self._subscriptions_caught_up(
                target_event_id=target_event_id,
                subscription_scope=subscription_scope,
            ):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._poll_interval_seconds)

    def delivery_retry_rate(self, window_seconds: float = 300.0) -> float:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COALESCE(
                            SUM(
                                CASE WHEN d.attempts > 1
                                THEN d.attempts - 1
                                ELSE 0
                                END
                            ), 0
                        ) AS retries,
                        COALESCE(
                            SUM(
                                CASE WHEN d.attempts > 0
                                THEN d.attempts ELSE 0 END
                            ), 0
                        ) AS attempted
                    FROM ah_bus_deliveries d
                    JOIN ah_bus_subscriptions s ON s.subscription_id = d.subscription_id
                    WHERE s.instance_id = %s
                      AND d.updated_at >= NOW() - (%s * INTERVAL '1 second')
                    """,
                    (self._instance_id, window_seconds),
                )
                row = cur.fetchone()
                if not row:
                    return 0.0
                retries = _as_int(row[0])
                attempted = _as_int(row[1])
                if attempted <= 0:
                    return 0.0
                return float(retries) / float(attempted)

    def drain(self, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = time.monotonic() + timeout_seconds
        while True:
            pending = sum(self.pending_deliveries().values())
            if pending == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._poll_interval_seconds)

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sub_ids = list(self._subs.keys())
            for sub in self._subs.values():
                sub.active = False
            threads = list(self._threads.values())
            self._subs.clear()
            self._threads.clear()
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                for sub_id in sub_ids:
                    cur.execute(
                        """
                        UPDATE ah_bus_subscriptions
                        SET active = FALSE
                        WHERE subscription_id = %s
                          AND instance_id = %s
                        """,
                        (sub_id, self._instance_id),
                    )
        if wait:
            for thread in threads:
                thread.join(timeout=2.0)

    def configure_acl(
        self,
        rules: Mapping[str, Sequence[str]] | None = None,
        *,
        enforce: bool = False,
    ) -> None:
        self._publish_acl = {
            topic: {publisher for publisher in publishers if publisher}
            for topic, publishers in (rules or {}).items()
        }
        self._enforce_acl = enforce

    def acl_status(self) -> MutableMapping[str, object]:
        return {
            "enforced": self._enforce_acl,
            "rule_count": len(self._publish_acl),
            "rules": sorted(self._publish_acl.keys()),
        }

    def _is_allowed(self, topic: str, publisher: str | None) -> bool:
        if not self._enforce_acl or not self._publish_acl:
            return True
        if not publisher:
            return False
        for pattern, allowed in self._publish_acl.items():
            if fnmatch.fnmatch(topic, pattern):
                return publisher in allowed
        return False

    def _poll_subscription(self, subscription: Subscription) -> None:
        while not self._closed and subscription.active:
            claimed = self._claim_next_delivery(subscription.id)
            if not claimed:
                time.sleep(self._poll_interval_seconds)
                continue
            delivery_id = _as_int(claimed["delivery_id"])
            event_id = _as_int(claimed["event_id"])
            envelope = Envelope(
                id=str(event_id),
                message=Message(
                    topic=str(claimed["topic"]),
                    payload=_decode_json(claimed["payload_json"]),
                    created_at=_coerce_timestamp(claimed["created_at"]),
                    metadata=_decode_json(claimed["metadata_json"]),
                ),
            )
            with self._lock:
                self._history.append(envelope)
            try:
                subscription.handler(envelope)
            except Exception as exc:
                self._logger.exception(
                    "postgres bus handler failed",
                    extra={"subscription_id": subscription.id, "event_id": event_id},
                )
                self._mark_retry(delivery_id, str(exc))
                continue
            self._mark_done(subscription.id, delivery_id, event_id)

    def _claim_next_delivery(self, subscription_id: str) -> Mapping[str, object] | None:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        d.delivery_id,
                        d.event_id,
                        e.topic,
                        e.payload_json,
                        e.metadata_json,
                        e.created_at
                    FROM ah_bus_deliveries d
                    JOIN ah_bus_events e ON e.event_id = d.event_id
                    JOIN ah_bus_subscriptions s ON s.subscription_id = d.subscription_id
                    WHERE d.subscription_id = %s
                      AND s.active = TRUE
                      AND s.instance_id = %s
                      AND d.status IN ('pending', 'retry')
                      AND d.next_attempt_at <= NOW()
                    ORDER BY d.event_id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """,
                    (subscription_id, self._instance_id),
                )
                row = cur.fetchone()
                if not row:
                    return None
                cur.execute(
                    """
                    UPDATE ah_bus_deliveries
                    SET status = 'processing', attempts = attempts + 1, updated_at = NOW()
                    WHERE delivery_id = %s
                    """,
                    (_as_int(row[0]),),
                )
                return {
                    "delivery_id": row[0],
                    "event_id": row[1],
                    "topic": row[2],
                    "payload_json": row[3],
                    "metadata_json": row[4],
                    "created_at": row[5],
                }

    def _mark_done(self, subscription_id: str, delivery_id: int, event_id: int) -> None:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ah_bus_deliveries
                    SET status = 'done', last_error = NULL, updated_at = NOW()
                    WHERE delivery_id = %s
                    """,
                    (delivery_id,),
                )
                cur.execute(
                    """
                    UPDATE ah_bus_subscriptions
                    SET cursor_event_id = GREATEST(cursor_event_id, %s), updated_at = NOW()
                    WHERE subscription_id = %s
                      AND instance_id = %s
                    """,
                    (event_id, subscription_id, self._instance_id),
                )

    def _mark_retry(self, delivery_id: int, error_message: str) -> None:
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ah_bus_deliveries
                    SET status = 'retry',
                        last_error = %s,
                        next_attempt_at = NOW() + (%s * INTERVAL '1 second'),
                        updated_at = NOW()
                    WHERE delivery_id = %s
                      AND subscription_id IN (
                          SELECT subscription_id
                          FROM ah_bus_subscriptions
                          WHERE instance_id = %s
                      )
                    """,
                    (
                        error_message[:1000],
                        self._retry_delay_seconds,
                        delivery_id,
                        self._instance_id,
                    ),
                )

    def _enqueue_replay_deliveries(
        self,
        cur: Any,
        subscription_id: str,
        *,
        topics: Sequence[str] | None,
        replay_last: int,
    ) -> None:
        cur.execute(
            """
            SELECT event_id, topic
            FROM ah_bus_events
            ORDER BY event_id DESC
            LIMIT %s
            """,
            (replay_last,),
        )
        rows = list(reversed(cur.fetchall()))
        for row in rows:
            event_id = _as_int(row[0])
            topic = str(row[1])
            if _matches(topic, topics):
                self._insert_pending_delivery(cur, subscription_id, event_id)

    def _enqueue_backlog_deliveries(
        self,
        cur: Any,
        subscription_id: str,
        *,
        topics: Sequence[str] | None,
        after_event_id: int,
    ) -> None:
        cur.execute(
            """
            SELECT event_id, topic
            FROM ah_bus_events
            WHERE event_id > %s
            ORDER BY event_id ASC
            """,
            (after_event_id,),
        )
        for row in cur.fetchall():
            event_id = _as_int(row[0])
            topic = str(row[1])
            if _matches(topic, topics):
                self._insert_pending_delivery(cur, subscription_id, event_id)

    def _insert_pending_delivery(self, cur: Any, subscription_id: str, event_id: int) -> None:
        cur.execute(
            """
            INSERT INTO ah_bus_deliveries (
                subscription_id,
                event_id,
                status,
                attempts,
                next_attempt_at,
                updated_at
            ) VALUES (%s, %s, 'pending', 0, NOW(), NOW())
            ON CONFLICT (subscription_id, event_id) DO NOTHING
            """,
            (subscription_id, event_id),
        )

    def _requeue_processing_deliveries(self, cur: Any, subscription_id: str) -> None:
        cur.execute(
            """
            UPDATE ah_bus_deliveries
            SET status = 'retry',
                next_attempt_at = NOW(),
                updated_at = NOW()
            WHERE subscription_id = %s
              AND status = 'processing'
            """,
            (subscription_id,),
        )

    def _subscriptions_caught_up(
        self,
        *,
        target_event_id: int,
        subscription_scope: Sequence[str] | None,
    ) -> bool:
        if target_event_id == 0:
            return True
        scope_ids = [sid for sid in (subscription_scope or []) if sid]
        with postgres_connection(self._dsn) as conn:
            with conn.cursor() as cur:
                if scope_ids:
                    cur.execute(
                        """
                        SELECT subscription_id, cursor_event_id
                        FROM ah_bus_subscriptions
                        WHERE active = TRUE
                          AND instance_id = %s
                          AND subscription_id IN (
                              SELECT UNNEST(%s::text[])
                          )
                        """,
                        (self._instance_id, scope_ids),
                    )
                else:
                    cur.execute(
                        """
                        SELECT subscription_id, cursor_event_id
                        FROM ah_bus_subscriptions
                        WHERE active = TRUE
                          AND instance_id = %s
                        """,
                        (self._instance_id,),
                    )
                rows = cur.fetchall()
                if not rows:
                    return True
                if scope_ids:
                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM ah_bus_deliveries d
                        JOIN ah_bus_subscriptions s ON s.subscription_id = d.subscription_id
                        WHERE s.instance_id = %s
                          AND s.active = TRUE
                          AND s.subscription_id IN (
                              SELECT UNNEST(%s::text[])
                          )
                          AND d.status IN ('pending', 'processing', 'retry')
                          AND d.event_id <= %s
                        """,
                        (self._instance_id, scope_ids, target_event_id),
                    )
                else:
                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM ah_bus_deliveries d
                        JOIN ah_bus_subscriptions s ON s.subscription_id = d.subscription_id
                        WHERE s.instance_id = %s
                          AND s.active = TRUE
                          AND d.status IN ('pending', 'processing', 'retry')
                          AND d.event_id <= %s
                        """,
                        (self._instance_id, target_event_id),
                    )
                pending_row = cur.fetchone()
                pending = _as_int(pending_row[0]) if pending_row else 0
                return pending == 0


def _encode_topics(topics: Sequence[str] | None) -> str:
    if topics is None:
        return "null"
    return json.dumps(list(topics))


def _decode_topics(raw: object) -> Sequence[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                return [str(item) for item in decoded]
            return None
        except json.JSONDecodeError:
            return None
    return None


def _matches(topic: str, topics: Sequence[str] | None) -> bool:
    if topics is None:
        return True
    if "*" in topics:
        return True
    return topic in topics


def _decode_json(raw: object) -> Mapping[str, object]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            pass
    return {}


def _coerce_timestamp(raw: object) -> datetime:
    if isinstance(raw, datetime):
        if raw.tzinfo:
            return raw.astimezone(timezone.utc)
        return raw.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        raise TypeError("boolean is not a valid integer value in this context")
    if isinstance(value, (int, str, float)):
        return int(value)
    raise TypeError(f"cannot coerce {type(value).__name__} to int")
