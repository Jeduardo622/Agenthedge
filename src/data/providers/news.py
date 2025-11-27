"""NewsAPI provider wrapper."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping

try:  # pragma: no cover
    from newsapi import NewsApiClient
except ImportError:  # pragma: no cover
    NewsApiClient = Any

from ..cache import TTLCache
from ..config import DataProviderConfig
from .base import BaseProvider, DataProviderError, MissingApiKeyError, TransientProviderError


class NewsProvider(BaseProvider):
    """Wraps NewsAPI to fetch topic and ticker news."""

    def __init__(
        self,
        config: DataProviderConfig,
        cache: TTLCache | None = None,
        *,
        client: NewsApiClient | None = None,
    ) -> None:
        if not config.news_api_key:
            raise MissingApiKeyError("News API key missing")
        super().__init__("newsapi", cache, rate_limit_per_minute=30)
        self._client = client or NewsApiClient(api_key=config.news_api_key)

    def ping(self) -> bool:  # pragma: no cover
        return True

    def get_company_news(
        self,
        symbol: str,
        *,
        language: str = "en",
        page_size: int = 50,
    ) -> List[Dict[str, Any]]:
        cache_key = self._cache_key("company", symbol, language, str(page_size))
        response: Dict[str, Any] = self.fetch_with_cache(
            cache_key,
            f"company news {symbol}",
            lambda: self._call(
                "company headlines",
                self._client.get_everything,
                q=symbol,
                language=language,
                sort_by="publishedAt",
                page_size=page_size,
            ),
        )
        return self._articles_from_payload(response)

    def search_topic(
        self,
        query: str,
        *,
        from_datetime: datetime | None = None,
        to_datetime: datetime | None = None,
        language: str = "en",
        page_size: int = 50,
    ) -> List[Dict[str, Any]]:
        cache_key = self._cache_key(
            "topic",
            query,
            from_datetime.isoformat() if from_datetime else "",
            to_datetime.isoformat() if to_datetime else "",
            language,
            str(page_size),
        )
        response: Dict[str, Any] = self.fetch_with_cache(
            cache_key,
            f"topic news {query}",
            lambda: self._call(
                "topic search",
                self._client.get_everything,
                q=query,
                language=language,
                from_param=from_datetime.isoformat() if from_datetime else None,
                to=to_datetime.isoformat() if to_datetime else None,
                page_size=page_size,
                sort_by="relevancy",
            ),
        )
        return self._articles_from_payload(response)

    def _articles_from_payload(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        articles = payload.get("articles", [])
        if articles in (None, []):
            return []
        if not isinstance(articles, list):
            raise DataProviderError("NewsAPI returned invalid articles payload")
        normalized: List[Dict[str, Any]] = []
        for article in articles:
            if not isinstance(article, Mapping):
                raise DataProviderError("NewsAPI article entry must be a mapping")
            normalized.append(dict(article))
        return normalized

    def _call(
        self, action: str, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Dict[str, Any]:
        def wrapped() -> Dict[str, Any]:
            try:
                payload = func(*args, **kwargs)
            except (TimeoutError, ConnectionError) as exc:
                raise TransientProviderError(str(exc)) from exc
            except Exception as exc:  # pragma: no cover
                raise DataProviderError(f"NewsAPI {action} failed") from exc
            if not isinstance(payload, dict):
                raise DataProviderError("NewsAPI returned unexpected payload")
            if payload.get("status") != "ok":
                raise DataProviderError(f"NewsAPI {action} failed: {payload}")
            return payload

        return self._execute(action, wrapped)
