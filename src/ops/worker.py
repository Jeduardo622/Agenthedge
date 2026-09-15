"""Sequential durable commands executed by one explicitly configured account worker."""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from agents.runtime import AgentRuntime
from ops.artifacts import InstalledArtifacts
from ops.commands import CommandStore, WorkerFenceError
from ops.fencing import WorkerLease
from ops.release_gate import ReleaseTrust


@dataclass(frozen=True)
class PaperTarget:
    store: CommandStore
    release: str

    def __post_init__(self) -> None:
        if type(self.store) is not CommandStore or self.store.mode != "paper_broker":
            raise ValueError("explicit separate paper account store required")
        if len(self.release) != 40 or any(c not in "0123456789abcdef" for c in self.release):
            raise ValueError("exact paper release required")


class DurableWorker:
    """No background thread, constructor activation, provider callback or implicit live target."""

    def __init__(
        self,
        store: CommandStore,
        runtime: AgentRuntime,
        *,
        trust: ReleaseTrust,
        installed: InstalledArtifacts,
        evidence_path: Path,
        paper: PaperTarget | None = None,
        lease_duration: timedelta = timedelta(seconds=60),
    ) -> None:
        if type(store) is not CommandStore or type(runtime) is not AgentRuntime:
            raise TypeError("actual CommandStore and AgentRuntime required")
        if not isinstance(trust, ReleaseTrust) or not isinstance(installed, InstalledArtifacts):
            raise TypeError("independent release trust and installed artifacts required")
        expected = trust.expected
        if (expected.account_id, expected.mode) != (store.account_id, store.mode):
            raise ValueError("worker trust namespace mismatch")
        if runtime._release_authorization.trust != trust:
            raise ValueError("runtime release authority differs from worker authority")
        runtime._release_authorization.bind_evidence_path(evidence_path)
        self.store, self.runtime, self.trust, self.installed = store, runtime, trust, installed
        self.paper, self.lease_duration = paper, lease_duration
        self.worker_id = uuid4().hex
        self.lease: WorkerLease | None = None
        self._serial = threading.Lock()
        self._running_command: str | None = None

    def acquire(self) -> None:
        if self.lease is None:
            self.installed.bind(self.runtime, self.trust)
        token = self.store.acquire_worker(
            worker_id=self.worker_id, release=self.trust.expected.sha, lease=self.lease_duration
        )
        if token is None:
            raise WorkerFenceError("another account worker owns the lease")
        lease = WorkerLease(self.store, self.worker_id, token, self.trust.expected.sha)
        if self.lease is None:
            self.runtime.bind_worker(lease)
        elif self.lease != lease:
            raise WorkerFenceError("expired worker cannot resume its old runtime")
        if self.lease is None:
            self.lease = lease

    def run_once(self) -> dict[str, Any] | None:
        if not self._serial.acquire(blocking=False):
            raise WorkerFenceError("worker loop is already executing")
        try:
            if self.lease is not None:
                self.lease.require_current()
            self.acquire()
            lease = self.lease
            assert lease is not None
            # A disconnected action is observed, never executed again.
            recoveries = self.store.recovery_commands()
            if recoveries:
                previous = recoveries[0]
                if previous["expected_release"] == lease.release:
                    claim = self.store.claim_recovery(
                        previous["command_id"],
                        worker_id=lease.worker_id,
                        fence_token=lease.fence_token,
                    )
                    self._process(claim, recovery=True)
            command = self.store.claim_next(
                worker_id=lease.worker_id, fence_token=lease.fence_token
            )
            if command is not None:
                return self._process(command, recovery=False)
            if self._running_command is not None:
                self._refresh_running()
            else:
                self._prepare_iteration()
                self.runtime.run_once(include_provider_health=False)
            return None
        finally:
            self._serial.release()

    def _refresh_running(self) -> None:
        lease = self.lease
        assert lease is not None and self._running_command is not None
        try:
            prepared = self._prepare_iteration()
            self.runtime.run_once(include_provider_health=False)
            observed = dict(self.runtime.control_readback("start"))
            if not prepared:
                observed = {
                    **observed,
                    "state": "RECOVERY_REQUIRED",
                    "unresolved": ["installed_artifacts_unavailable"],
                }
        except WorkerFenceError:
            raise
        except Exception as exc:
            observed = {
                "state": "RECOVERY_REQUIRED",
                "unresolved": ["controller_readback_failed"],
                "error_type": type(exc).__name__,
            }
        running = observed["state"] in {"RUNNING_PAPER", "RUNNING_LIVE"}
        self.store.record_observation(
            self._running_command,
            worker_id=lease.worker_id,
            fence_token=lease.fence_token,
            state="succeeded" if running else "recovery_required",
            details=observed,
            refresh_running=True,
        )
        if not running:
            self._running_command = None

    def _prepare_iteration(self) -> bool:
        """A failed artifact check stops new ticks while recovery keeps running."""
        try:
            self._refresh_evidence()
            self.installed.refresh(self.runtime, self.trust)
            return True
        except WorkerFenceError:
            raise
        except Exception:
            self.runtime._control_running = False
            return False

    def _refresh_evidence(self, *, recover: bool = False) -> None:
        clock = self.runtime._agent_extras.get("now")
        now = clock() if callable(clock) else None
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("aware worker decision clock required")
        self.runtime._release_authorization.refresh_evidence(now=now, recover=recover)

    def _process(self, command: dict[str, Any], *, recovery: bool) -> dict[str, Any]:
        lease = self.lease
        assert lease is not None
        action, key = command["action"], command["command_id"]
        expected = {
            "start_paper": "RUNNING_PAPER",
            "request_live_start": "RUNNING_LIVE",
            "halt": "HALTED",
            "close_session": "CLOSED",
            "reconcile": "RECONCILED",
            "rollback_to_paper": "ROLLED_BACK_PAPER",
        }[action]
        state = "recovery_required"
        try:
            lease.require_current()
            if action in {"start_paper", "request_live_start"}:
                if recovery:
                    details = dict(self.runtime.control_readback("start"))
                else:
                    if self.store.recovery_commands():
                        raise RuntimeError("uncertain earlier action requires observation")
                    self._refresh_evidence(recover=True)
                    self.installed.require(self.runtime, self.trust)
                    self.installed.refresh(self.runtime, self.trust)
                    self.runtime.control_rearm(key)
                    self.installed.activate(self.runtime, self.trust)
                    details = dict(self.runtime.control_start())
                if details["state"] == expected:
                    self._running_command = key
            elif action == "reconcile":
                details = dict(
                    self.runtime.control_readback("reconcile")
                    if recovery
                    else self.runtime.control_preflight(key)
                )
            else:
                self._running_command = None
                if not recovery:
                    self.runtime.control_halt(key)
                details = dict(
                    self.runtime.control_close_session(key)
                    if action == "close_session"
                    else self.runtime.control_readback(action)
                )
                if action == "rollback_to_paper" and details["state"] == "HALTED":
                    details = self._rollback(command, details)
            if details["state"] == expected and not details["unresolved"]:
                state = "succeeded"
        except WorkerFenceError:
            raise  # Leave acknowledged action uncertain for the next owner.
        except Exception as exc:
            details = {"reason": "controller_action_failed", "error_type": type(exc).__name__}
        self.store.record_observation(
            key,
            worker_id=lease.worker_id,
            fence_token=lease.fence_token,
            state=state,
            details=details,
        )
        return self.store.status(key) or {}

    def _rollback(self, command: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
        if self.paper is None or self.store.mode != "live":
            raise RuntimeError("explicit separate paper target required")
        linked = (
            "rollback-paper-"
            + hashlib.sha256(
                f"{self.store.account_id}:{command['command_id']}".encode()
            ).hexdigest()
        )
        self.paper.store.submit(
            command_id=linked,
            account_id=self.paper.store.account_id,
            mode="paper_broker",
            action="start_paper",
            expected_release=self.paper.release,
            authorization={
                "linked_live_account": self.store.account_id,
                "linked_rollback": command["command_id"],
            },
        )
        observed = self.paper.store.running_observation(
            release=self.paper.release, command_id=linked
        )
        if observed is None or observed.get("state") != "RUNNING_PAPER":
            return {
                **live,
                "state": "RECOVERY_REQUIRED",
                "unresolved": ["paper_start_pending"],
                "paper_command_id": linked,
            }
        return {**live, "state": "ROLLED_BACK_PAPER", "paper": observed, "paper_command_id": linked}
