"""Local dispatch/halt sequencing within the required single-owner worker process.

Durable journal checks remain authoritative across restarts. This gate never holds
a database transaction and cannot coordinate an unsupported second worker process.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock, RLock
from typing import Iterator


class SubmissionBlocked(RuntimeError):
    """A halt request invalidated or could not safely fence a dispatch."""


@dataclass(frozen=True)
class DispatchTicket:
    gate: "SubmissionGate"
    generation: int
    allow_halted: bool

    def require_current(self) -> None:
        self.gate.require_ticket(self.generation, allow_halted=self.allow_halted)


class SubmissionGate:
    def __init__(self) -> None:
        self._dispatch = RLock()
        self._state = Lock()
        self._generation = 0
        self._inhibited: set[int] = set()

    @contextmanager
    def dispatch(self, *, allow_halted: bool = False) -> Iterator[DispatchTicket]:
        # There is one sequential execution consumer. Unexpected concurrent sends
        # fail closed rather than waiting with already sampled authority/deadlines.
        if not self._dispatch.acquire(blocking=False):
            raise SubmissionBlocked("another dispatch or halt claim is active")
        try:
            with self._state:
                ticket = DispatchTicket(self, self._generation, allow_halted)
            ticket.require_current()
            yield ticket
        finally:
            self._dispatch.release()

    def require_ticket(self, generation: int, *, allow_halted: bool) -> None:
        with self._state:
            if not allow_halted and (self._inhibited or generation != self._generation):
                raise SubmissionBlocked("halt invalidated submission authority")

    @contextmanager
    def halt_claim(self, *, timeout: float) -> Iterator[None]:
        # Inhibit before waiting for an already-entered request. Each request owns
        # its marker; a successful request cannot clear another failed/pending one.
        with self._state:
            self._generation += 1
            request = self._generation
            self._inhibited.add(request)
        if not self._dispatch.acquire(timeout=max(0.0, timeout)):
            raise SubmissionBlocked("halt dispatch wait expired")
        try:
            yield
        except BaseException:
            raise  # Preserve inhibition when a durable claim did not complete.
        else:
            with self._state:
                self._inhibited.remove(request)
        finally:
            self._dispatch.release()
