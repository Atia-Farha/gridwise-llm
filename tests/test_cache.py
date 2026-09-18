"""Cache resilience tests.

Redis is not gated on at startup, so the service must behave correctly while
it is absent, while it is broken mid-request, and once it comes back.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services import cache


@pytest.fixture(autouse=True)
def _reset():
    cache.clear_memory()
    yield
    cache.clear_memory()


class TestCacheKey:
    def test_same_inputs_give_the_same_key(self):
        a = cache.cache_key(["note"], 200.0, 40.0)
        b = cache.cache_key(["note"], 200.0, 40.0)
        assert a == b

    def test_battery_capacity_changes_the_key(self):
        """"50% of capacity" resolves to different kWh per battery, so two
        scenarios with identical notes must not share an entry."""
        a = cache.cache_key(["Keep 50% in reserve"], 200.0, 40.0)
        b = cache.cache_key(["Keep 50% in reserve"], 400.0, 40.0)
        assert a != b

    def test_note_order_changes_the_key(self):
        assert cache.cache_key(["a", "b"], 200.0, 40.0) != cache.cache_key(
            ["b", "a"], 200.0, 40.0
        )

    def test_model_is_namespaced(self):
        assert settings.openai_model in cache.cache_key(["n"], 1.0, 0.0)


class TestInProcessFallback:
    @pytest.mark.asyncio
    async def test_roundtrip_without_redis(self, monkeypatch):
        monkeypatch.setattr(settings, "redis_url", "")
        key = cache.cache_key(["note"], 200.0, 40.0)
        assert await cache.get(key) is None

        payload = [{"note_index": 0, "directive_type": "no_op"}]
        await cache.set(key, payload)
        assert await cache.get(key) == payload

    @pytest.mark.asyncio
    async def test_unreachable_redis_does_not_raise(self, monkeypatch):
        """A wrong password or a down container must degrade, not fail."""
        monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
        cache._retry_after = 0.0

        key = cache.cache_key(["note"], 200.0, 40.0)
        assert await cache.get(key) is None  # miss, no exception

        payload = [{"note_index": 0, "directive_type": "no_op"}]
        await cache.set(key, payload)  # must not raise
        assert await cache.get(key) == payload  # served from memory

    @pytest.mark.asyncio
    async def test_failed_connect_backs_off_then_retries(self, monkeypatch):
        """A failure must not disable the shared cache for the whole process."""
        monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
        cache._retry_after = 0.0

        await cache.get(cache.cache_key(["n"], 1.0, 0.0))
        assert cache._retry_after > 0, "a cooldown should be armed"

        # Inside the cooldown the connection is not retried…
        assert await cache._get_redis() is None
        # …and once it lapses, a fresh attempt is made rather than staying off.
        cache._retry_after = 0.0
        assert await cache._get_redis() is None
        assert cache._retry_after > 0


class TestMemoryBounds:
    @pytest.mark.asyncio
    async def test_lru_eviction_keeps_memory_bounded(self, monkeypatch):
        monkeypatch.setattr(settings, "redis_url", "")
        for i in range(cache._MEMORY_MAX_ENTRIES + 25):
            await cache.set(f"k{i}", [{"note_index": i}])
        assert len(cache._memory) <= cache._MEMORY_MAX_ENTRIES
        assert await cache.get("k0") is None          # oldest evicted
        assert await cache.get(f"k{cache._MEMORY_MAX_ENTRIES + 24}") is not None
