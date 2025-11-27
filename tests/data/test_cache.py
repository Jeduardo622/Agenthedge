from data.cache import TTLCache


def test_cache_hit_and_expiration(monkeypatch):
    current_time = 1_000.0

    def fake_time():
        return current_time

    monkeypatch.setattr("data.cache.time.time", fake_time)
    cache = TTLCache(ttl_seconds=5, max_items=4)
    cache.set("alpha", 123)
    assert cache.get("alpha") == (True, 123)

    current_time += 10
    assert cache.get("alpha") == (False, None)


def test_cache_evicts_stalest_entry(monkeypatch):
    current_time = 1_000.0

    def fake_time():
        return current_time

    monkeypatch.setattr("data.cache.time.time", fake_time)
    cache = TTLCache(ttl_seconds=100, max_items=2)

    cache.set("a", 1)
    current_time += 1
    cache.set("b", 2)
    current_time += 1
    cache.set("c", 3)  # should evict the stalest (`a`)

    assert cache.get("a") == (False, None)
    assert cache.get("b")[0]
    assert cache.get("c")[0]


def test_cache_disabled_returns_bypass():
    cache = TTLCache(ttl_seconds=1, max_items=1, enabled=False)
    cache.set("x", 7)
    assert cache.get("x") == (False, None)
    result = cache.cached("key", lambda: 99)
    assert result == 99
