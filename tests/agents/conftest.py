from __future__ import annotations

import threading
from typing import Iterator

import pytest

from agents.messaging import MessageBus


class OwnedMessageBuses:
    def __init__(self) -> None:
        self._buses: list[MessageBus] = []
        self._worker_threads: list[threading.Thread] = []

    def add(self, bus: MessageBus) -> None:
        self._buses.append(bus)

    def close_all(self) -> None:
        for bus in reversed(self._buses):
            self._worker_threads.extend(
                worker._thread for worker in bus._workers.values()  # type: ignore[attr-defined]
            )
            bus.close()
        self._buses.clear()

    def workers_are_stopped(self) -> bool:
        return all(not thread.is_alive() for thread in self._worker_threads)


@pytest.fixture
def owned_message_buses(monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnedMessageBuses]:
    owner = OwnedMessageBuses()
    original_init = MessageBus.__init__

    def owned_init(self: MessageBus, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)
        owner.add(self)

    monkeypatch.setattr(MessageBus, "__init__", owned_init)
    yield owner
    owner.close_all()
    assert owner.workers_are_stopped()
