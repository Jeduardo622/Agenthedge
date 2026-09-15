"""Validated point-in-time research dataset bundles."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence, cast

PRICE_CONVENTIONS = {"raw", "split_adjusted", "total_return_adjusted"}
UNIVERSE_POLICIES = {"point_in_time", "static"}


def _checksum(value: object, name: str) -> str:
    result = _text(value, name)
    if len(result) != 64 or any(ch not in "0123456789abcdef" for ch in result):
        raise ValueError(f"dataset {name} must be a lowercase SHA-256 digest")
    return result


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"dataset {name} must be nonempty text")
    return value.strip()


def _time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"dataset {name} must be an ISO timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"dataset {name} must be an ISO timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"dataset {name} timestamp must include timezone")
    return result.astimezone(timezone.utc)


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"dataset {name} must be finite decimal text")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"dataset {name} must be finite decimal text") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"dataset {name} must be finite{' positive' if positive else ''}")
    return result


def _validated_record(record: Mapping[str, object]) -> dict[str, object]:
    required = {
        "record_id",
        "kind",
        "symbol",
        "event_at",
        "available_at",
        "source",
        "revision",
        "checksum",
    }
    if not isinstance(record, Mapping) or not required <= record.keys():
        raise ValueError("dataset record missing required provenance")
    result = dict(record)
    for field in ("record_id", "kind", "symbol", "source", "revision"):
        result[field] = _text(result[field], field)
    result["checksum"] = _checksum(result["checksum"], "checksum")
    result["symbol"] = str(result["symbol"]).upper()
    _time(result["event_at"], "event_at")
    _time(result["available_at"], "available_at")
    kind = result["kind"]
    if kind == "price":
        try:
            date.fromisoformat(_text(result.get("session"), "session"))
        except ValueError as exc:
            raise ValueError("dataset price session must be an ISO date") from exc
        for field in ("open", "high", "low", "close"):
            _decimal(result.get(field), field, positive=True)
        if "reference_close" in result:
            _decimal(result["reference_close"], "reference_close", positive=True)
        if result.get("volume") is not None:
            volume = _decimal(result["volume"], "volume")
            if volume < 0:
                raise ValueError("dataset volume must be finite and nonnegative")
    elif kind == "universe":
        if type(result.get("member")) is not bool:
            raise ValueError("universe membership must be boolean")
        _time(result.get("effective_at"), "effective_at")
    elif kind == "corporate_action":
        action_application_time(result)
    elif kind == "risk_classification":
        if result.get("asset_type") not in {"equity", "etf"}:
            raise ValueError("unsupported risk asset type")
        if (result.get("asset_type") == "equity") != bool(result.get("sector")):
            raise ValueError("equity classification alone requires a sector")
    elif kind == "risk_liquidity":
        _decimal(result.get("average_daily_volume"), "average_daily_volume", positive=True)
    elif kind == "etf_sector_map":
        weights = result.get("weights")
        if not isinstance(weights, Mapping):
            raise ValueError("ETF sector weights must be a mapping")
        total = sum(
            (_decimal(item, "ETF sector weight", positive=True) for item in weights.values()),
            Decimal("0"),
        )
        if total != 1:
            raise ValueError("ETF sector weights must sum to 1")
        date.fromisoformat(_text(result.get("as_of"), "ETF as_of"))
    elif kind not in {"fundamental", "news"}:
        raise ValueError("unsupported dataset record kind")
    return result


def records_checksum(records: Sequence[Mapping[str, object]]) -> str:
    normalized = [_validated_record(item) for item in records]
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def visible_records(
    records: tuple[Mapping[str, object], ...], at: datetime
) -> tuple[Mapping[str, object], ...]:
    cutoff = (
        at.astimezone(timezone.utc)
        if at.tzinfo is not None and at.utcoffset() is not None
        else None
    )
    if cutoff is None:
        raise ValueError("visibility cutoff must include timezone")
    grouped: dict[str, list[dict[str, object]]] = {}
    for raw in records:
        record = _validated_record(raw)
        if _time(record["available_at"], "available_at") <= cutoff:
            grouped.setdefault(str(record["record_id"]), []).append(record)
    selected: list[dict[str, object]] = []
    for versions in grouped.values():
        identities = {
            (str(item["kind"]), str(item["symbol"]), str(item["event_at"])) for item in versions
        }
        if len(identities) != 1:
            raise ValueError("record identity reused across incompatible observations")
        latest = max(_time(item["available_at"], "available_at") for item in versions)
        candidates = [
            item for item in versions if _time(item["available_at"], "available_at") == latest
        ]
        canonical = {json.dumps(item, sort_keys=True, separators=(",", ":")) for item in candidates}
        if len(canonical) != 1:
            raise ValueError("ambiguous revision at the same availability time")
        selected.append(candidates[0])
    return tuple(
        sorted(
            selected,
            key=lambda item: (str(item["kind"]), str(item["symbol"]), str(item["record_id"])),
        )
    )


def visible_universe(records: tuple[Mapping[str, object], ...], at: datetime) -> tuple[str, ...]:
    cutoff = (
        at.astimezone(timezone.utc)
        if at.tzinfo is not None and at.utcoffset() is not None
        else None
    )
    if cutoff is None:
        raise ValueError("visibility cutoff must include timezone")
    latest: dict[str, tuple[datetime, bool]] = {}
    for item in visible_records(records, cutoff):
        if item["kind"] != "universe":
            continue
        effective = _time(item["effective_at"], "effective_at")
        if effective <= cutoff:
            symbol = str(item["symbol"])
            prior = latest.get(symbol)
            if prior is None or effective > prior[0]:
                latest[symbol] = (effective, bool(item["member"]))
            elif effective == prior[0] and bool(item["member"]) != prior[1]:
                raise ValueError("ambiguous universe membership at effective time")
    return tuple(sorted(symbol for symbol, (_, member) in latest.items() if member))


def action_application_time(record: Mapping[str, object]) -> datetime:
    if record.get("action_type") == "split":
        _decimal(record.get("ratio"), "split ratio", positive=True)
        action_time = _time(record.get("effective_at"), "effective_at")
    elif record.get("action_type") == "cash_dividend":
        _decimal(record.get("amount"), "dividend amount", positive=True)
        if record.get("currency") != "USD":
            raise ValueError("only explicitly USD cash dividends are supported")
        entitlement = _time(record.get("entitlement_at"), "entitlement_at")
        action_time = _time(record.get("payable_at"), "payable_at")
        if entitlement > action_time:
            raise ValueError("dividend entitlement must not follow payable time")
    else:
        raise ValueError("unsupported corporate action")
    available = _time(record.get("available_at"), "available_at")
    return max(action_time, available)


def validate_adjustment_compatibility(
    price_convention: str, actions: Sequence[Mapping[str, object]]
) -> None:
    if price_convention not in PRICE_CONVENTIONS:
        raise ValueError("unsupported price convention")
    for item in actions:
        action = item.get("action_type")
        if price_convention == "split_adjusted" and action == "split":
            raise ValueError("split-adjusted prices would double apply split action")
        if price_convention == "total_return_adjusted" and action in {"split", "cash_dividend"}:
            raise ValueError("total-return-adjusted prices would double apply corporate action")


@dataclass(frozen=True)
class DatasetManifest:
    schema_version: int
    dataset_id: str
    created_at: datetime
    source: str
    license: str
    records_checksum: str
    price_convention: str
    calendar: str
    universe_policy: str
    coverage_start: date
    coverage_end: date
    limitations: tuple[str, ...]
    risk_contract: Mapping[str, Any] | None = None

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "created_at": self.created_at.isoformat(),
            "source": self.source,
            "license": self.license,
            "records_checksum": self.records_checksum,
            "price_convention": self.price_convention,
            "calendar": self.calendar,
            "universe_policy": self.universe_policy,
            "coverage_start": self.coverage_start.isoformat(),
            "coverage_end": self.coverage_end.isoformat(),
            "limitations": list(self.limitations),
            **({"risk_contract": dict(self.risk_contract)} if self.risk_contract else {}),
        }
        if self.risk_contract is not None:
            from risk.policy import RiskPolicy

            policy = self.risk_contract.get("policy")
            if isinstance(policy, Mapping):
                result["risk_policy_hash"] = RiskPolicy.from_mapping(dict(policy)).content_hash
                result["risk_policy_status"] = "proposed_unapproved"
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "DatasetManifest":
        if value.get("schema_version") != 1:
            raise ValueError("unsupported dataset manifest schema")
        limitations = value.get("limitations")
        if not isinstance(limitations, list) or any(
            not isinstance(item, str) or not item.strip() for item in limitations
        ):
            raise ValueError("dataset limitations must be explicit")
        convention = _text(value.get("price_convention"), "price_convention")
        if convention not in PRICE_CONVENTIONS:
            raise ValueError("unsupported price convention")
        universe = _text(value.get("universe_policy"), "universe_policy")
        if universe not in UNIVERSE_POLICIES:
            raise ValueError("unsupported universe policy")
        if universe == "static" and not any("survivorship" in item.lower() for item in limitations):
            raise ValueError("static universe requires explicit survivorship limitation")
        try:
            start = date.fromisoformat(_text(value.get("coverage_start"), "coverage_start"))
            end = date.fromisoformat(_text(value.get("coverage_end"), "coverage_end"))
        except ValueError as exc:
            raise ValueError("invalid dataset coverage dates") from exc
        if start > end:
            raise ValueError("dataset coverage start exceeds end")
        if value.get("calendar") != "XNYS":
            raise ValueError("qualified dataset calendar must be XNYS")
        risk_contract = value.get("risk_contract")
        if risk_contract is not None and not isinstance(risk_contract, Mapping):
            raise ValueError("risk_contract must be a mapping")
        return cls(
            1,
            _text(value.get("dataset_id"), "dataset_id"),
            _time(value.get("created_at"), "created_at"),
            _text(value.get("source"), "source"),
            _text(value.get("license"), "license"),
            _checksum(value.get("records_checksum"), "records_checksum"),
            convention,
            "XNYS",
            universe,
            start,
            end,
            tuple(item.strip() for item in limitations),
            MappingProxyType(dict(risk_contract)) if risk_contract is not None else None,
        )


@dataclass(frozen=True)
class PointInTimeDataset:
    manifest: DatasetManifest
    records: tuple[Mapping[str, object], ...]


def load_dataset_bundle(path: str | Path) -> PointInTimeDataset:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("unable to load qualified dataset bundle") from exc
    if not isinstance(payload, Mapping) or set(payload) != {"manifest", "records"}:
        raise ValueError("invalid qualified dataset bundle")
    if not isinstance(payload["manifest"], Mapping) or not isinstance(payload["records"], list):
        raise ValueError("invalid qualified dataset bundle")
    manifest = DatasetManifest.from_mapping(payload["manifest"])
    records = tuple(_validated_record(item) for item in payload["records"])
    if records_checksum(records) != manifest.records_checksum:
        raise ValueError("dataset records checksum mismatch")
    actions = tuple(item for item in records if item["kind"] == "corporate_action")
    validate_adjustment_compatibility(manifest.price_convention, actions)
    if manifest.risk_contract is not None and any(
        item["kind"] == "price" and "reference_close" not in item for item in records
    ):
        raise ValueError("qualified risk price records require reference_close")
    return PointInTimeDataset(manifest, tuple(MappingProxyType(item) for item in records))


def qualified_risk_service_factory(bundle: PointInTimeDataset) -> Any:
    """Build the engine factory only when the bundle has complete sourced risk inputs."""
    from portfolio.accounting import AccountingState, PositionState
    from risk.evaluator import (
        FreshnessThresholds,
        MarketRiskInputs,
        SourcedClassification,
        SourcedLiquidity,
        SourcedMark,
    )
    from risk.policy import EtfSectorMap, RiskPolicy
    from risk.service import RiskEvaluationService

    contract = bundle.manifest.risk_contract
    if contract is None or set(contract) != {
        "policy",
        "freshness_seconds",
        "artifact_ttl_seconds",
        "reference_price_convention",
    }:
        return None
    if contract["reference_price_convention"] != "split_adjusted":
        raise ValueError("qualified risk requires split_adjusted reference prices")
    if not isinstance(contract["policy"], dict) or not isinstance(
        contract["freshness_seconds"], dict
    ):
        raise ValueError("qualified risk policy and freshness must be explicit mappings")
    policy = RiskPolicy.from_mapping(contract["policy"])
    freshness = contract["freshness_seconds"]
    thresholds = FreshnessThresholds(
        timedelta(seconds=float(_decimal(freshness.get("mark"), "mark freshness", positive=True))),
        timedelta(
            seconds=float(
                _decimal(freshness.get("classification"), "classification freshness", positive=True)
            )
        ),
        timedelta(
            seconds=float(
                _decimal(freshness.get("liquidity"), "liquidity freshness", positive=True)
            )
        ),
    )
    ttl = timedelta(
        seconds=float(_decimal(contract["artifact_ttl_seconds"], "artifact TTL", positive=True))
    )

    def factory(store: Any, broker: Any, clock: Any) -> Any:
        def state() -> AccountingState:
            projection = store.projection()
            return AccountingState(
                Decimal(projection["cash"]),
                Decimal(projection["realized_pnl"]),
                {
                    symbol: PositionState(Decimal(item["quantity"]), Decimal(item["average_cost"]))
                    for symbol, item in projection["positions"].items()
                },
            )

        def market(at: datetime) -> MarketRiskInputs:
            visible = visible_records(bundle.records, at)
            latest: dict[tuple[str, str], Mapping[str, object]] = {}
            for item in visible:
                if item["kind"] not in {
                    "price",
                    "risk_classification",
                    "risk_liquidity",
                    "etf_sector_map",
                }:
                    continue
                key = (str(item["kind"]), str(item["symbol"]))
                observed = _time(item["event_at"], "event_at")
                prior = latest.get(key)
                if prior is not None and observed == _time(prior["event_at"], "event_at"):
                    if dict(item) != dict(prior):
                        raise ValueError("ambiguous sourced risk record at observed time")
                    continue
                if prior is None or observed > _time(prior["event_at"], "event_at"):
                    latest[key] = item
            marks, classifications, liquidity, funds = {}, {}, {}, {}
            etf_dates, etf_sources, etf_hashes = [], [], []
            for (kind, symbol), item in latest.items():
                observed, available = _time(item["event_at"], "event_at"), _time(
                    item["available_at"], "available_at"
                )
                provenance = (observed, available, str(item["source"]), str(item["checksum"]))
                if kind == "price":
                    marks[symbol] = SourcedMark(cast(Any, item["close"]), *provenance)
                elif kind == "risk_classification":
                    classifications[symbol] = SourcedClassification(
                        cast(Any, item["asset_type"]), cast(Any, item.get("sector")), *provenance
                    )
                elif kind == "risk_liquidity":
                    liquidity[symbol] = SourcedLiquidity(
                        cast(Any, item["average_daily_volume"]), *provenance
                    )
                elif kind == "etf_sector_map":
                    item_as_of = date.fromisoformat(str(item["as_of"]))
                    age_days = (at.date() - item_as_of).days
                    if age_days < 0:
                        raise ValueError("ETF sector mapping is not yet available")
                    if age_days > policy.etf_sector_map_max_age_days:
                        raise ValueError("ETF sector mapping is stale")
                    funds[symbol] = dict(cast(Mapping[str, object], item["weights"]))
                    etf_dates.append(item_as_of)
                    etf_sources.append(str(item["source"]))
                    etf_hashes.append(str(item["checksum"]))
            etf = EtfSectorMap.from_mapping(
                {
                    "schema_version": 1,
                    "status": "available" if funds else "unavailable",
                    "source": ",".join(sorted(etf_sources)) if funds else None,
                    "as_of": min(etf_dates) if funds else None,
                    "checksum": (
                        hashlib.sha256("".join(sorted(etf_hashes)).encode()).hexdigest()
                        if funds
                        else None
                    ),
                    "funds": funds,
                }
            )
            return MarketRiskInputs(at, marks, classifications, liquidity, etf)

        return RiskEvaluationService(
            policy=policy,
            thresholds=thresholds,
            market_inputs=market,
            accounting_state=state,
            reservations=broker.working_reservations,
            now=clock.now,
            artifact_ttl=ttl,
        )

    return factory
