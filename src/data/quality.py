"""Data quality checks for ingestion snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Iterable, List, Mapping


@dataclass(frozen=True)
class DataQualityIssue:
    data_type: str
    reason: str
    severity: str


class DataQualityChecker:
    """Runs lightweight quality checks over ingestion payloads."""

    def __init__(
        self,
        *,
        quote_freshness_seconds: int = 300,
        news_freshness_seconds: int = 3600,
        outlier_pct_threshold: float = 0.15,
    ) -> None:
        self.quote_freshness_seconds = max(1, quote_freshness_seconds)
        self.news_freshness_seconds = max(1, news_freshness_seconds)
        self.outlier_pct_threshold = max(0.0, outlier_pct_threshold)

    def check_quote(
        self, quote: Mapping[str, Any], *, now: datetime | None = None
    ) -> List[DataQualityIssue]:
        issues: List[DataQualityIssue] = []
        close = quote.get("c")
        if not isinstance(close, (int, float)):
            issues.append(DataQualityIssue("quote", "missing_close_price", "error"))
        elif isinstance(close, bool) or not isfinite(float(close)) or float(close) <= 0:
            issues.append(DataQualityIssue("quote", "invalid_close_price", "error"))
        prev_close = quote.get("pc")
        if (
            not isinstance(prev_close, (int, float))
            or isinstance(prev_close, bool)
            or not isfinite(float(prev_close))
            or float(prev_close) <= 0
        ):
            issues.append(DataQualityIssue("quote", "invalid_previous_close", "error"))
        if (
            isinstance(close, (int, float))
            and not isinstance(close, bool)
            and isfinite(float(close))
            and isinstance(prev_close, (int, float))
            and not isinstance(prev_close, bool)
            and isfinite(float(prev_close))
            and float(prev_close) > 0
        ):
            delta_pct = abs((float(close) - float(prev_close)) / float(prev_close))
            if delta_pct > self.outlier_pct_threshold:
                issues.append(DataQualityIssue("quote", "outlier_price_change", "warning"))
        quote_time = _parse_provider_time(quote.get("t"))
        if quote_time is None:
            issues.append(DataQualityIssue("quote", "missing_quote_timestamp", "error"))
        else:
            checked_at = _require_utc_now(now)
            age = (checked_at - quote_time).total_seconds()
            if age < 0:
                issues.append(DataQualityIssue("quote", "future_quote", "error"))
            elif age > self.quote_freshness_seconds:
                issues.append(DataQualityIssue("quote", "stale_quote", "error"))
        return issues

    def check_news(
        self, news: Iterable[Mapping[str, Any]], *, now: datetime | None = None
    ) -> List[DataQualityIssue]:
        issues: List[DataQualityIssue] = []
        now = _require_utc_now(now)
        for item in news:
            published = item.get("publishedAt")
            if not isinstance(published, str):
                continue
            parsed = _parse_iso(published)
            if not parsed:
                continue
            age = (now - parsed).total_seconds()
            if age > self.news_freshness_seconds:
                issues.append(DataQualityIssue("news", "stale_news_item", "warning"))
                break
        return issues

    def check_fundamentals(self, fundamentals: Mapping[str, Any]) -> List[DataQualityIssue]:
        if not fundamentals:
            return [DataQualityIssue("fundamentals", "empty_fundamentals", "warning")]
        return []


def _parse_iso(value: str) -> datetime | None:
    candidate = value
    if value.endswith("Z"):
        candidate = f"{value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_provider_time(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and isfinite(float(value)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _require_utc_now(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("now must be UTC-aware")
    return result.astimezone(timezone.utc)
