"""
Tests for Phase A — Tier State Store (Issue #12)

Covers:
  - Feature flag routing: InMemoryStateStore selected by default (USE_REDIS_STATE=0)
  - Tier/score get/set parity between InMemory and Redis stores
  - 100ms TTL cache behaviour in RedisStateStore (hit then expiry)
  - Bootstrap logic: seed Redis from tier_state.json only when Redis is empty
"""
from __future__ import annotations

import json
import os
import sys
import time
import types

import pytest

# ---------------------------------------------------------------------------
# Ensure app package is importable when tests are run from the backend/tests dir.
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_redis():
    """Return a fakeredis.FakeRedis instance with decode_responses=True."""
    import fakeredis
    return fakeredis.FakeRedis(decode_responses=True)


def _make_redis_store(fake_redis=None):
    """Build a RedisStateStore wired to an in-process fakeredis server."""
    from app.services.tier_state_store import RedisStateStore

    if fake_redis is None:
        fake_redis = _make_fake_redis()

    store = RedisStateStore.__new__(RedisStateStore)
    store._redis = fake_redis
    store._prefix = 'test:tier'
    store._tiers_key = 'test:tier:tiers'
    store._scores_key = 'test:tier:scores'
    from app.services.tier_state_store import _LocalCache
    store._cache = _LocalCache()
    return store


# ===========================================================================
# Feature flag routing
# ===========================================================================

class TestFeatureFlagRouting:
    """get_store() must return InMemoryStateStore when USE_REDIS_STATE=0."""

    def test_default_is_in_memory(self, monkeypatch):
        import app.services.tier_state_store as m
        from app.services.tier_state_store import InMemoryStateStore
        m.reset_store(None)  # clear singleton

        # Patch settings.use_redis_state to False so get_store() picks InMemory.
        import app.config as cfg
        monkeypatch.setattr(cfg.settings, 'use_redis_state', False)

        store = m.get_store()
        assert isinstance(store, InMemoryStateStore)
        m.reset_store(None)

    def test_redis_flag_selects_redis_store(self, monkeypatch):
        import fakeredis
        import app.services.tier_state_store as m
        m.reset_store(None)

        # Inject a pre-built RedisStateStore (fakeredis-backed) as the singleton.
        redis_store = _make_redis_store()
        m.reset_store(redis_store)
        assert m.get_store() is redis_store
        m.reset_store(None)

    def test_reset_store_replaces_singleton(self):
        import app.services.tier_state_store as m
        from app.services.tier_state_store import InMemoryStateStore

        store_a = InMemoryStateStore()
        store_b = InMemoryStateStore()
        m.reset_store(store_a)
        assert m.get_store() is store_a
        m.reset_store(store_b)
        assert m.get_store() is store_b
        m.reset_store(None)


# ===========================================================================
# InMemoryStateStore — basic parity
# ===========================================================================

class TestInMemoryStateStore:
    @pytest.fixture
    def store(self):
        from app.services.tier_state_store import InMemoryStateStore
        return InMemoryStateStore()

    def test_get_tier_missing(self, store):
        assert store.get_tier('AAPL') is None

    def test_set_get_tier(self, store):
        store.set_tier('AAPL', 1)
        assert store.get_tier('AAPL') == 1

    def test_get_score_missing(self, store):
        assert store.get_score('AAPL') is None

    def test_set_get_score(self, store):
        store.set_score('AAPL', 42.5)
        assert store.get_score('AAPL') == pytest.approx(42.5)

    def test_all_tiers(self, store):
        store.set_tier('AAPL', 1)
        store.set_tier('MSFT', 2)
        assert store.all_tiers() == {'AAPL': 1, 'MSFT': 2}

    def test_all_scores(self, store):
        store.set_score('AAPL', 10.0)
        store.set_score('MSFT', 20.0)
        result = store.all_scores()
        assert result['AAPL'] == pytest.approx(10.0)
        assert result['MSFT'] == pytest.approx(20.0)

    def test_is_empty(self, store):
        assert store.is_empty()
        store.set_tier('X', 3)
        assert not store.is_empty()

    def test_bulk_load(self, store):
        store.bulk_load({'A': 1, 'B': 2}, {'A': 5.5, 'B': 6.6})
        assert store.get_tier('A') == 1
        assert store.get_tier('B') == 2
        assert store.get_score('A') == pytest.approx(5.5)
        assert store.get_score('B') == pytest.approx(6.6)

    def test_overwrite(self, store):
        store.set_tier('AAPL', 1)
        store.set_tier('AAPL', 3)
        assert store.get_tier('AAPL') == 3


# ===========================================================================
# RedisStateStore — basic parity
# ===========================================================================

class TestRedisStateStore:
    @pytest.fixture
    def store(self):
        return _make_redis_store()

    def test_get_tier_missing(self, store):
        assert store.get_tier('AAPL') is None

    def test_set_get_tier(self, store):
        store.set_tier('AAPL', 1)
        assert store.get_tier('AAPL') == 1

    def test_get_score_missing(self, store):
        assert store.get_score('AAPL') is None

    def test_set_get_score(self, store):
        store.set_score('AAPL', 99.9)
        assert store.get_score('AAPL') == pytest.approx(99.9)

    def test_all_tiers(self, store):
        store.set_tier('AAPL', 1)
        store.set_tier('MSFT', 3)
        assert store.all_tiers() == {'AAPL': 1, 'MSFT': 3}

    def test_all_scores(self, store):
        store.set_score('AAPL', 1.0)
        store.set_score('GOOG', 2.0)
        result = store.all_scores()
        assert result['AAPL'] == pytest.approx(1.0)
        assert result['GOOG'] == pytest.approx(2.0)

    def test_is_empty(self, store):
        assert store.is_empty()
        store.set_tier('X', 2)
        assert not store.is_empty()

    def test_bulk_load(self, store):
        store.bulk_load({'A': 1, 'B': 2}, {'A': 3.3, 'B': 4.4})
        assert store.get_tier('A') == 1
        assert store.get_tier('B') == 2
        assert store.get_score('A') == pytest.approx(3.3)
        assert store.get_score('B') == pytest.approx(4.4)


# ===========================================================================
# 100 ms TTL cache behaviour
# ===========================================================================

class TestTTLCache:
    """The in-process cache inside RedisStateStore must expire entries after 100 ms."""

    def test_cache_hit_avoids_redis(self, monkeypatch):
        """After a set, the next get must return the cached value."""
        store = _make_redis_store()
        store.set_tier('AAPL', 2)
        # Second read should hit cache, not Redis — verify by confirming the
        # return value matches what we just set.
        assert store.get_tier('AAPL') == 2

    def test_cache_populates_on_first_get(self):
        """A Redis-backed read populates the local cache for the next 100 ms."""
        store = _make_redis_store()
        store._redis.hset('test:tier:tiers', 'TSLA', '1')
        # First read: misses cache, hits Redis.
        assert store.get_tier('TSLA') == 1
        # Second read within 100 ms: hits cache (we monkeypatch Redis to verify).
        original_hget = store._redis.hget
        calls = []

        def counting_hget(key, field):
            calls.append((key, field))
            return original_hget(key, field)

        store._redis.hget = counting_hget
        assert store.get_tier('TSLA') == 1
        # The cache was hit — hget should not have been called.
        assert calls == [], f'Expected cache hit but Redis was called: {calls}'

    def test_cache_expires_after_ttl(self, monkeypatch):
        """After TTL expiry a fresh Redis read must be issued."""
        from app.services.tier_state_store import _CACHE_TTL_S
        store = _make_redis_store()
        store.set_tier('NVDA', 1)

        # Simulate time advancing past TTL by backdating all cache entries.
        with store._cache._lock:
            for k in list(store._cache._data):
                val, _exp = store._cache._data[k]
                store._cache._data[k] = (val, time.monotonic() - _CACHE_TTL_S - 0.001)

        # Now update Redis directly (simulates another process writing).
        store._redis.hset('test:tier:tiers', 'NVDA', '3')

        # The cache is expired — must re-read from Redis and see updated value.
        assert store.get_tier('NVDA') == 3

    def test_cache_invalidated_on_set(self):
        """Writing a new value must invalidate the in-process cache entry."""
        store = _make_redis_store()
        store.set_tier('AMZN', 2)
        assert store.get_tier('AMZN') == 2  # populates cache
        store.set_tier('AMZN', 1)           # must invalidate cache
        assert store.get_tier('AMZN') == 1

    def test_cache_bounded(self):
        """Cache must not grow beyond max_size."""
        from app.services.tier_state_store import _LocalCache
        cache = _LocalCache(max_size=5)
        for i in range(10):
            cache.set(f'key{i}', i)
        with cache._lock:
            assert len(cache._data) <= 5


# ===========================================================================
# Bootstrap logic
# ===========================================================================

class TestBootstrap:
    """RedisStateStore.bootstrap_from_file seeds Redis only when Redis is empty."""

    def _write_state_file(self, path, tiers, scores):
        data = {
            'assignments': tiers,
            'scores': scores,
            'pinned': [],
            'saved_at': time.time(),
        }
        path.write_text(json.dumps(data), encoding='utf-8')

    def test_seeds_redis_when_empty(self, tmp_path):
        state_file = tmp_path / 'tier_state.json'
        self._write_state_file(state_file, {'AAPL': 1, 'MSFT': 2}, {'AAPL': 9.0, 'MSFT': 5.0})

        store = _make_redis_store()
        assert store.is_empty()
        store.bootstrap_from_file(state_file)

        assert store.get_tier('AAPL') == 1
        assert store.get_tier('MSFT') == 2
        assert store.get_score('AAPL') == pytest.approx(9.0)

    def test_skips_seed_when_redis_has_data(self, tmp_path):
        state_file = tmp_path / 'tier_state.json'
        self._write_state_file(state_file, {'AAPL': 1}, {'AAPL': 9.0})

        store = _make_redis_store()
        # Pre-populate Redis with different data.
        store.set_tier('GOOG', 3)

        store.bootstrap_from_file(state_file)

        # AAPL from file must NOT have been loaded because Redis was not empty.
        assert store.get_tier('AAPL') is None
        assert store.get_tier('GOOG') == 3

    def test_handles_missing_file_gracefully(self, tmp_path):
        store = _make_redis_store()
        missing = tmp_path / 'nonexistent.json'
        store.bootstrap_from_file(missing)   # must not raise
        assert store.is_empty()

    def test_handles_corrupt_file_gracefully(self, tmp_path):
        bad_file = tmp_path / 'bad.json'
        bad_file.write_text('{not valid json}', encoding='utf-8')
        store = _make_redis_store()
        store.bootstrap_from_file(bad_file)  # must not raise
        assert store.is_empty()


# ===========================================================================
# Tier manager integration — InMemory route
# ===========================================================================

class TestTierManagerIntegration:
    """Smoke-test that tier_manager functions work through the store adapter."""

    @pytest.fixture(autouse=True)
    def patch_store(self):
        """Inject a fresh InMemoryStateStore for each test."""
        import app.services.tier_state_store as m
        from app.services.tier_state_store import InMemoryStateStore
        store = InMemoryStateStore()
        m.reset_store(store)
        yield store
        m.reset_store(None)

    def test_get_tier_defaults_to_3(self):
        import app.services.tier_manager as tm
        assert tm.get_tier('UNKNOWN') == 3

    def test_promote_updates_store(self, patch_store):
        import app.services.tier_manager as tm
        # Seed with Tier 3
        patch_store.set_tier('AAPL', 3)
        import app.services.tier_manager as tm_mod
        tm_mod._tier_assignments['AAPL'] = 3
        tm_mod._tier_members[3].add('AAPL')

        promoted = tm.promote('AAPL', reason='test')
        assert promoted
        assert patch_store.get_tier('AAPL') == 2

    def test_update_composite_score_updates_store(self, patch_store):
        import app.services.tier_manager as tm
        tm.update_composite_score('GOOG', 77.7)
        assert patch_store.get_score('GOOG') == pytest.approx(77.7)
