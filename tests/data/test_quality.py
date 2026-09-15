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


def test_quote_requires_finite_positive_price_and_current_provider_time() -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    checker = DataQualityChecker(quote_freshness_seconds=60)

    assert {
        i.reason
        for i in checker.check_quote({"c": float("nan"), "pc": 100, "t": now.timestamp()}, now=now)
    } == {"invalid_close_price"}
    assert "missing_quote_timestamp" in {
        i.reason for i in checker.check_quote({"c": 100, "pc": 99}, now=now)
    }
    assert "stale_quote" in {
        i.reason
        for i in checker.check_quote(
            {"c": 100, "pc": 99, "t": (now - timedelta(seconds=61)).timestamp()}, now=now
        )
    }
    assert "future_quote" in {
        i.reason
        for i in checker.check_quote(
            {"c": 100, "pc": 99, "t": (now + timedelta(seconds=1)).timestamp()}, now=now
        )
    }
    assert "invalid_previous_close" in {
        i.reason
        for i in checker.check_quote({"c": 100, "pc": float("inf"), "t": now.timestamp()}, now=now)
    }
    assert "missing_quote_timestamp" in {
        i.reason for i in checker.check_quote({"c": 100, "pc": 99, "t": now.isoformat()}, now=now)
    }
