"""In-memory message bus with pub/sub semantics."""

from __future__ import annotations

import fnmatch
import logging
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, List, Mapping, MutableMapping, Sequence, cast

Payload = Mapping[str, object] | None
MessageHandler = Callable[["Envelope"], None]


@dataclass(frozen=True, slots=True)
class Message:
    topic: str
    payload: Payload
    created_at: datetime
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Envelope:
    id: str
    message: Message


@dataclass(slots=True)
class Subscription:
    id: str
    topics: Sequence[str] | None
    handler: MessageHandler
    active: bool = True

    def matches(self, topic: str) -> bool:
        if not self.active:
            return False
        if self.topics is None:
            return True
        if "*" in self.topics:
            return True
        return topic in self.topics


class _SubscriptionWorker:
    def __init__(self, subscription: Subscription, logger: logging.Logger) -> None:
        self._subscription = subscription
        self._logger = logger
        self._queue: queue.Queue[Envelope | object] = queue.Queue()
        self._stop_token = object()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"MessageBusSub-{subscription.id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, envelope: Envelope) -> None:
        if self._closed:
            return
        self._queue.put(envelope)

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(self._stop_token)

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)

    def pending(self) -> int:
        return self._queue.unfinished_tasks

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._stop_token:
                    return
                envelope = cast(Envelope, item)
                if not self._subscription.active:
                    continue
                try:
                    self._subscription.handler(envelope)
                except Exception:
                    self._logger.exception(
                        "message handler failed",
                        extra={
                            "subscription_id": self._subscription.id,
                            "topic": envelope.message.topic,
                        },
                    )
            finally:
                self._queue.task_done()


class MessageBus:
    """Thread-safe pub/sub bus with bounded replay buffer."""

    def __init__(
        self,
        *,
        max_history: int = 512,
    ) -> None:
        self._logger = logging.getLogger("agenthedge.message_bus")
        self._subs: Dict[str, Subscription] = {}
        self._workers: Dict[str, _SubscriptionWorker] = {}
        self._history: Deque[Envelope] = deque(maxlen=max_history)
        self._lock = threading.RLock()
        self._publish_acl: Dict[str, set[str]] = {}
        self._enforce_acl = False
        self._closed = False

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
        envelope = Envelope(
            id=str(uuid.uuid4()),
            message=Message(
                topic=topic,
                payload=payload or {},
                created_at=datetime.now(timezone.utc),
                metadata={
                    **(metadata or {}),
                    "publisher": publisher,
                },
            ),
        )
        with self._lock:
            self._history.append(envelope)
            workers = [
                self._workers[sub_id]
                for sub_id, sub in self._subs.items()
                if sub.matches(topic) and sub_id in self._workers
            ]
        for worker in workers:
            worker.enqueue(envelope)
        return envelope

    def subscribe(
        self,
        handler: MessageHandler,
        *,
        topics: Sequence[str] | None = None,
        replay_last: int = 0,
    ) -> Subscription:
        if self._closed:
            raise RuntimeError("MessageBus is closed")
        subscription = Subscription(id=str(uuid.uuid4()), topics=topics, handler=handler)
        worker = _SubscriptionWorker(subscription, self._logger)
        with self._lock:
            self._subs[subscription.id] = subscription
            self._workers[subscription.id] = worker
            history = list(self._history)
        if replay_last > 0:
            for envelope in history[-replay_last:]:
                if subscription.matches(envelope.message.topic):
                    worker.enqueue(envelope)
        return subscription

    def unsubscribe(self, subscription_id: str) -> None:
        worker: _SubscriptionWorker | None = None
        with self._lock:
            sub = self._subs.pop(subscription_id, None)
            worker = self._workers.pop(subscription_id, None)
            if sub:
                sub.active = False
        if worker:
            worker.stop()
            worker.join(timeout=2.0)

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
        workers: List[_SubscriptionWorker] = []
        with self._lock:
            self._history.clear()
            for sub in self._subs.values():
                sub.active = False
            workers = list(self._workers.values())
            self._subs.clear()
            self._workers.clear()
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(timeout=2.0)

    def depth(self) -> int:
        with self._lock:
            return len(self._history)

    def caught_up_checkpoint(self) -> int:
        with self._lock:
            if not self._history:
                return 0
            latest = self._history[-1]
            try:
                return int(latest.id)
            except ValueError:
                return len(self._history)

    def pending_deliveries(self) -> Dict[str, int]:
        with self._lock:
            return {
                sub_id: worker.pending()
                for sub_id, worker in self._workers.items()
                if worker.pending() > 0
            }

    def drain(self, timeout_seconds: float) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = time.monotonic() + timeout_seconds
        while True:
            with self._lock:
                pending = sum(worker.pending() for worker in self._workers.values())
            if pending == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def close(self, *, wait: bool = True) -> None:
        workers: List[_SubscriptionWorker] = []
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for sub in self._subs.values():
                sub.active = False
            workers = list(self._workers.values())
            self._subs.clear()
            self._workers.clear()
        for worker in workers:
            worker.stop()
        if wait:
            for worker in workers:
                worker.join(timeout=2.0)

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
