"""Atomic one-process EconomicEvent storage for deterministic simulations."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, cast

from portfolio.accounting import AccountingState
from portfolio.journal import (
    EconomicEvent,
    economic_event_from_record,
    economic_event_record,
    project_economic_events,
)
from portfolio.store import PortfolioSnapshot, PortfolioStore, Position


class LocalEconomicEventStore(PortfolioStore):
    """Persist canonical events and their projection in one atomic JSON file.

    One process owns the file. The lock only coordinates threads using this instance.
    Genesis and namespace are explicit and must match on every reopen.
    """

    _SCHEMA_VERSION = 1

    def __init__(
        self,
        path: str | Path,
        *,
        genesis: AccountingState,
        account_id: str,
        mode: str,
    ) -> None:
        if not isinstance(genesis, AccountingState):
            raise TypeError("explicit AccountingState genesis required")
        self._validate_namespace(account_id, mode)
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._genesis = genesis
        self._account_id = account_id
        self._mode = mode
        self._lock = threading.RLock()
        self._recovery_required = False
        self._events: tuple[EconomicEvent, ...] = ()
        self._projection = project_economic_events(genesis, ())
        if self._path.exists():
            self._load()
        else:
            self._persist_economic_state((), self._projection)

    def apply_event(self, event: EconomicEvent) -> bool:
        if not isinstance(event, EconomicEvent):
            raise TypeError("EconomicEvent required")
        if (event.account_id, event.mode) != (self._account_id, self._mode):
            raise ValueError("economic event namespace differs from local store")
        record = economic_event_record(event)
        with self._lock:
            if self._recovery_required:
                raise RuntimeError("local economic event store requires recovery")
            for prior in self._events:
                if prior.event_id != event.event_id:
                    continue
                if economic_event_record(prior) != record:
                    raise ValueError("economic event identity conflict; recovery required")
                return False
            candidate_events = (*self._events, event)
            candidate_projection = project_economic_events(self._genesis, candidate_events)
            try:
                self._persist_economic_state(candidate_events, candidate_projection)
            except Exception:
                try:
                    self._load()
                except Exception:
                    self._recovery_required = True
                raise
            self._events = candidate_events
            self._projection = candidate_projection
            return True

    def projection(self) -> dict[str, Any]:
        with self._lock:
            return cast(dict[str, Any], json.loads(json.dumps(self._projection)))

    def projection_at(self, at: datetime) -> dict[str, Any]:
        if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("economic projection cutoff must be timezone-aware")
        with self._lock:
            projected = project_economic_events(
                self._genesis, tuple(item for item in self._events if item.occurred_at <= at)
            )
            return cast(dict[str, Any], json.loads(json.dumps(projected)))

    def events(self) -> tuple[EconomicEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def snapshot(self) -> PortfolioSnapshot:
        """Expose the legacy float facade while retaining Decimal event storage."""
        with self._lock:
            positions = {
                symbol: Position(
                    symbol,
                    float(value["quantity"]),
                    float(value["average_cost"]),
                )
                for symbol, value in self._projection["positions"].items()
            }
            return PortfolioSnapshot(
                float(self._projection["cash"]),
                float(self._projection["realized_pnl"]),
                positions,
                self._events[-1].occurred_at.isoformat() if self._events else "",
            )

    def apply_fill(self, **kwargs: object) -> Mapping[str, float]:
        raise RuntimeError("local economic store requires canonical EconomicEvent writes")

    def snapshot_dict(self) -> MutableMapping[str, object]:
        snapshot = self.snapshot()
        return {
            "cash": snapshot.cash,
            "realized_pnl": snapshot.realized_pnl,
            "positions": {
                symbol: {
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "average_cost": position.average_cost,
                }
                for symbol, position in snapshot.positions.items()
            },
            "last_updated": snapshot.last_updated,
        }

    def bulk_load(self, positions: Iterable[Position], *, cash: float | None = None) -> None:
        raise RuntimeError("local economic store cannot adopt legacy float state")

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid local economic event store; recovery required") from exc
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "namespace",
            "genesis",
            "events",
            "projection",
        }:
            raise ValueError("invalid local economic event store; recovery required")
        namespace = raw["namespace"]
        if not isinstance(namespace, Mapping) or set(namespace) != {"account_id", "mode"}:
            raise ValueError("invalid local economic namespace; recovery required")
        if (namespace["account_id"], namespace["mode"]) != (self._account_id, self._mode):
            raise ValueError("local economic namespace differs; recovery required")
        if raw["schema_version"] != self._SCHEMA_VERSION:
            raise ValueError("unsupported local economic event store schema")
        canonical_genesis = project_economic_events(self._genesis, ())
        if raw["genesis"] != canonical_genesis:
            raise ValueError("local economic genesis differs; recovery required")
        records = raw["events"]
        if not isinstance(records, list):
            raise ValueError("invalid local economic events; recovery required")
        events = tuple(economic_event_from_record(item) for item in records)
        if any((item.account_id, item.mode) != (self._account_id, self._mode) for item in events):
            raise ValueError("local economic namespace differs; recovery required")
        projection = project_economic_events(self._genesis, events)
        if raw["projection"] != projection:
            raise ValueError("local economic projection differs; recovery required")
        self._events = events
        self._projection = projection

    def _persist_economic_state(
        self, events: tuple[EconomicEvent, ...], projection: Mapping[str, Any]
    ) -> None:
        payload = {
            "schema_version": self._SCHEMA_VERSION,
            "namespace": {"account_id": self._account_id, "mode": self._mode},
            "genesis": project_economic_events(self._genesis, ()),
            "events": [economic_event_record(item) for item in events],
            "projection": dict(projection),
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=self._path.name + ".", suffix=".tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _validate_namespace(account_id: str, mode: str) -> None:
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("explicit local economic account namespace required")
        if mode not in {"simulated", "paper_broker", "live"}:
            raise ValueError("explicit valid local economic mode required")
