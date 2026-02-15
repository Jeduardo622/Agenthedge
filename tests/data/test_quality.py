from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data.quality import DataQualityChecker


def test_quality_checker_flags_missing_quote_close() -> None:
    checker = DataQualityChecker()
    issues = checker.check_quote({"pc": 100.0})
    assert any(issue.reason == "missing_close_price" for issue in issues)


def test_quality_checker_flags_outlier_price_move() -> None:
    checker = DataQualityChecker(outlier_pct_threshold=0.05)
    issues = checker.check_quote({"c": 120.0, "pc": 100.0})
    assert any(issue.reason == "outlier_price_change" for issue in issues)


def test_quality_checker_flags_stale_news_item() -> None:
    checker = DataQualityChecker(news_freshness_seconds=60)
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    issues = checker.check_news([{"publishedAt": stale}])
    assert any(issue.reason == "stale_news_item" for issue in issues)
