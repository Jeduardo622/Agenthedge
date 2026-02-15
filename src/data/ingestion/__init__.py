"""High-level data ingestion service plumbing multiple providers."""

from .service import DataIngestionService, MarketSnapshot

__all__ = ["DataIngestionService", "MarketSnapshot"]
