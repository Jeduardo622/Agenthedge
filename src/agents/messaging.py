"""In-memory message bus with pub/sub semantics."""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, List, Mapping, MutableMapping, Sequence

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


class MessageBus:
    """Thread-safe pub/sub bus with bounded replay buffer."""

    def __init__(self, *, max_history: int = 512) -> None:
        self._subs: Dict[str, Subscription] = {}
        self._history: Deque[Envelope] = deque(maxlen=max_history)
        self._lock = threading.RLock()

    def publish(
        self,
        topic: str,
        payload: Payload = None,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> Envelope:
        envelope = Envelope(
            id=str(uuid.uuid4()),
            message=Message(
                topic=topic,
                payload=payload or {},
                created_at=datetime.now(timezone.utc),
                metadata=metadata or {},
            ),
        )
        with self._lock:
            self._history.append(envelope)
            subs = list(self._subs.values())
        for sub in subs:
            if sub.matches(topic):
                sub.handler(envelope)
        return envelope

    def subscribe(
        self,
        handler: MessageHandler,
        *,
        topics: Sequence[str] | None = None,
        replay_last: int = 0,
    ) -> Subscription:
        subscription = Subscription(id=str(uuid.uuid4()), topics=topics, handler=handler)
        with self._lock:
            self._subs[subscription.id] = subscription
            history = list(self._history)
        if replay_last > 0:
            for envelope in history[-replay_last:]:
                if subscription.matches(envelope.message.topic):
                    handler(envelope)
        return subscription

    def unsubscribe(self, subscription_id: str) -> None:
        with self._lock:
            sub = self._subs.pop(subscription_id, None)
            if sub:
                sub.active = False

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
            self._history.clear()
            self._subs.clear()

    def depth(self) -> int:
        with self._lock:
            return len(self._history)
