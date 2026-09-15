"""Concrete account-worker capability; no message payload may create authority."""

from dataclasses import dataclass
from time import monotonic

from ops.commands import CommandStore, WorkerFenceError, _sha, _text


@dataclass(frozen=True)
class WorkerLeaseDeadline:
    expires_monotonic: float

    def require_current(self) -> None:
        if monotonic() >= self.expires_monotonic:
            raise WorkerFenceError("worker lease expired before broker submission")


@dataclass(frozen=True)
class WorkerLease:
    store: CommandStore
    worker_id: str
    fence_token: int
    release: str

    def __post_init__(self) -> None:
        if type(self.store) is not CommandStore:
            raise TypeError("actual CommandStore required")
        _text(self.worker_id)
        _sha(self.release)
        if type(self.fence_token) is not int or self.fence_token <= 0:
            raise ValueError("positive worker fencing token required")

    def require_current(self) -> WorkerLeaseDeadline:
        started = monotonic()
        status = self.store.require_worker_lease(
            worker_id=self.worker_id, fence_token=self.fence_token
        )
        if status.release != self.release:
            raise WorkerFenceError("worker release does not match installed release")
        return WorkerLeaseDeadline(started + status.remaining.total_seconds())
