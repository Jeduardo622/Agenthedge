from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("POSTGRES_DSN not set; skipping Postgres integration tests")
    return dsn
