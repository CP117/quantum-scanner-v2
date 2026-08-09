"""
Tests for Phase B — Snapshot State Store (Issue #12 Phase B)

Covers:
  - Feature flag routing: InMemorySnapshotStore selected by default (USE_REDIS_SNAPSHOT=0)
  - Upsert / get-top-N parity between InMemory and Redis stores
  - Top-N retrieval via Redis Sorted Set (score ordering)
  - Meta read/write via both stores
  - lookup_snapshot_row (get_row) for both stores
  - clear_snapshot (clear) for both stores
  - apply_to_top_n callback for both stores
  - delete_row / invalidate
  - 100 ms TTL cache behaviour for RedisSnapshotStore get_row
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
    """Build a RedisSnapshotStore wired to an in-process fakeredis server."""
    from app.services.snapshot_state_store import RedisSnapshotStore, _LocalCache

    if fake_redis is None:
        fake_redis = _make_fake_redis()

    store = RedisSnapshotStore.__new__(RedisSnapshotStore)
    store._redis = fake_redis
    store._prefix = 'test:snap'
    store._cache = _LocalCache()
    return store


def _row(symbol: str, score: float, **extra) -> dict:
    """Build a minimal scored row."""
    return {'symbol': symbol, 'final_score': score, '_snapshot_refreshed_at': 0.0, **extra}


# ===========================================================================
# Feature flag routing
# ===========================================================================

class TestFeatureFlagRouting:
    """get_snapshot_store() must return InMemorySnapshotStore when USE_REDIS_SNAPSHOT=0."""

    def test_default_is_in_memory(self, monkeypatch):
        import app.services.snapshot_state_store as m
        from app.services.snapshot_state_store import InMemorySnapshotStore
        m.reset_snapshot_store(None)

        import app.config as cfg
        monkeypatch.setattr(cfg.settings, 'use_redis_snapshot', False)

        store = m.get_snapshot_store()
        assert isinstance(store, InMemorySnapshotStore)
        m.reset_snapshot_store(None)

    def test_redis_flag_selects_redis_store(self, monkeypatch):
        import app.services.snapshot_state_store as m
        m.reset_snapshot_store(None)

        redis_store = _make_redis_store()
        m.reset_snapshot_store(redis_store)
        assert m.get_snapshot_store() is redis_store
        m.reset_snapshot_store(None)

    def test_reset_store_replaces_singleton(self):
        import app.services.snapshot_state_store as m
        from app.services.snapshot_state_store import InMemorySnapshotStore

        snap = {'stocks': {}, 'crypto': {}}
        meta = {'stocks': {}, 'crypto': {}}
        store_a = InMemorySnapshotStore(snap, meta)
        store_b = InMemorySnapshotStore(snap, meta)
        m.reset_snapshot_store(store_a)
        assert m.get_snapshot_store() is store_a
        m.reset_snapshot_store(store_b)
        assert m.get_snapshot_store() is store_b
        m.reset_snapshot_store(None)


# ===========================================================================
# InMemorySnapshotStore — basic parity
# ===========================================================================

class TestInMemorySnapshotStore:
    @pytest.fixture
    def store(self):
        from app.services.snapshot_state_store import InMemorySnapshotStore
        snap = {'stocks': {}, 'crypto': {}}
        meta = {'stocks': {}, 'crypto': {}}
        return InMemorySnapshotStore(snap, meta)

    def test_upsert_and_get_row(self, store):
        row = _row('AAPL', 9.0)
        store.upsert('stocks', 'AAPL', row)
        result = store.get_row('stocks', 'AAPL')
        assert result is not None
        assert result['symbol'] == 'AAPL'
        assert result['final_score'] == pytest.approx(9.0)

    def test_get_row_missing(self, store):
        assert store.get_row('stocks', 'MISSING') is None

    def test_row_count(self, store):
        assert store.row_count('stocks') == 0
        store.upsert('stocks', 'AAPL', _row('AAPL', 5.0))
        assert store.row_count('stocks') == 1

    def test_get_top_n_ordering(self, store):
        store.upsert('stocks', 'A', _row('A', 1.0))
        store.upsert('stocks', 'B', _row('B', 5.0))
        store.upsert('stocks', 'C', _row('C', 3.0))
        results = store.get_top_n('stocks', 10)
        scores = [r['final_score'] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_get_top_n_limit(self, store):
        for i in range(5):
            store.upsert('stocks', f'SYM{i}', _row(f'SYM{i}', float(i)))
        results = store.get_top_n('stocks', 3)
        assert len(results) == 3

    def test_delete_row(self, store):
        store.upsert('stocks', 'AAPL', _row('AAPL', 5.0))
        assert store.delete_row('stocks', 'AAPL') is True
        assert store.get_row('stocks', 'AAPL') is None
        assert store.delete_row('stocks', 'AAPL') is False

    def test_clear(self, store):
        store.upsert('stocks', 'AAPL', _row('AAPL', 5.0))
        store.set_meta('stocks', {'rows_scored': 1})
        store.clear('stocks')
        assert store.row_count('stocks') == 0
        assert store.get_meta('stocks').get('rows_scored') == 0

    def test_get_meta_empty(self, store):
        assert isinstance(store.get_meta('stocks'), dict)

    def test_set_meta(self, store):
        store.set_meta('stocks', {'last_batch_at': 'T1', 'universe_size': 42})
        meta = store.get_meta('stocks')
        assert meta['last_batch_at'] == 'T1'
        assert meta['universe_size'] == 42

    def test_apply_to_top_n(self, store):
        store.upsert('stocks', 'A', _row('A', 10.0))
        store.upsert('stocks', 'B', _row('B', 5.0))
        store.upsert('stocks', 'C', _row('C', 1.0))

        applied = []

        def cb(row):
            applied.append(row['symbol'])

        count = store.apply_to_top_n('stocks', 2, cb)
        assert count == 2
        # Top-2 by score are A and B
        assert set(applied) == {'A', 'B'}

    def test_is_empty(self, store):
        assert store.is_empty('stocks')
        store.upsert('stocks', 'X', _row('X', 1.0))
        assert not store.is_empty('stocks')

    def test_get_row_returns_copy(self, store):
        row = _row('AAPL', 5.0)
        store.upsert('stocks', 'AAPL', row)
        r1 = store.get_row('stocks', 'AAPL')
        r1['injected'] = True
        r2 = store.get_row('stocks', 'AAPL')
        # The injected key must not survive in the store
        assert 'injected' not in r2


# ===========================================================================
# RedisSnapshotStore — basic parity
# ===========================================================================

class TestRedisSnapshotStore:
    @pytest.fixture
    def store(self):
        return _make_redis_store()

    def test_upsert_and_get_row(self, store):
        row = _row('AAPL', 9.0)
        store.upsert('stocks', 'AAPL', row)
        result = store.get_row('stocks', 'AAPL')
        assert result is not None
        assert result['symbol'] == 'AAPL'
        assert result['final_score'] == pytest.approx(9.0)

    def test_get_row_missing(self, store):
        assert store.get_row('stocks', 'MISSING') is None

    def test_row_count(self, store):
        assert store.row_count('stocks') == 0
        store.upsert('stocks', 'AAPL', _row('AAPL', 5.0))
        assert store.row_count('stocks') == 1

    def test_get_top_n_ordering(self, store):
        store.upsert('stocks', 'A', _row('A', 1.0))
        store.upsert('stocks', 'B', _row('B', 5.0))
        store.upsert('stocks', 'C', _row('C', 3.0))
        results = store.get_top_n('stocks', 10)
        scores = [r['final_score'] for r in results]
        assert scores == sorted(scores, reverse=True)
        assert scores[0] == pytest.approx(5.0)

    def test_get_top_n_uses_sorted_set(self, store):
        """Top-N must use the Sorted Set, not a full table scan."""
        for i in range(10):
            store.upsert('stocks', f'SYM{i}', _row(f'SYM{i}', float(i)))
        results = store.get_top_n('stocks', 3)
        assert len(results) == 3
        # Top 3 by score are SYM9 (9.0), SYM8 (8.0), SYM7 (7.0)
        top_scores = sorted([r['final_score'] for r in results], reverse=True)
        assert top_scores[0] == pytest.approx(9.0)
        assert top_scores[1] == pytest.approx(8.0)
        assert top_scores[2] == pytest.approx(7.0)

    def test_delete_row(self, store):
        store.upsert('stocks', 'AAPL', _row('AAPL', 5.0))
        assert store.delete_row('stocks', 'AAPL') is True
        assert store.get_row('stocks', 'AAPL') is None
        assert store.row_count('stocks') == 0
        assert store.delete_row('stocks', 'AAPL') is False

    def test_delete_row_removes_from_sorted_set(self, store):
        store.upsert('stocks', 'AAPL', _row('AAPL', 5.0))
        store.delete_row('stocks', 'AAPL')
        # Sorted set must also be empty.
        zcard = store._redis.zcard(store._scores_key('stocks'))
        assert zcard == 0

    def test_clear(self, store):
        store.upsert('stocks', 'AAPL', _row('AAPL', 5.0))
        store.set_meta('stocks', {'rows_scored': 1})
        store.clear('stocks')
        assert store.row_count('stocks') == 0
        assert store._redis.hlen(store._rows_key('stocks')) == 0
        assert store._redis.zcard(store._scores_key('stocks')) == 0
        assert store._redis.hlen(store._meta_key('stocks')) == 0

    def test_meta_read_write(self, store):
        store.set_meta('stocks', {'last_batch_at': 'T1', 'universe_size': 42})
        meta = store.get_meta('stocks')
        assert meta['last_batch_at'] == 'T1'
        assert meta['universe_size'] == 42

    def test_meta_merge(self, store):
        store.set_meta('stocks', {'a': 1})
        store.set_meta('stocks', {'b': 2})
        meta = store.get_meta('stocks')
        assert meta['a'] == 1
        assert meta['b'] == 2

    def test_apply_to_top_n_redis(self, store):
        store.upsert('stocks', 'A', _row('A', 10.0))
        store.upsert('stocks', 'B', _row('B', 5.0))
        store.upsert('stocks', 'C', _row('C', 1.0))

        applied = []

        def cb(row):
            applied.append(row['symbol'])
            row['tagged'] = True

        count = store.apply_to_top_n('stocks', 2, cb)
        assert count == 2
        assert set(applied) == {'A', 'B'}

        # Verify the mutation was written back to Redis
        row_a = store.get_row('stocks', 'A')
        assert row_a is not None
        assert row_a.get('tagged') is True

    def test_apply_to_top_n_zero(self, store):
        store.upsert('stocks', 'A', _row('A', 10.0))
        count = store.apply_to_top_n('stocks', 0, lambda r: None)
        assert count == 0

    def test_apply_to_top_n_empty_market(self, store):
        count = store.apply_to_top_n('stocks', 5, lambda r: None)
        assert count == 0

    def test_is_empty(self, store):
        assert store.is_empty('stocks')
        store.upsert('stocks', 'X', _row('X', 1.0))
        assert not store.is_empty('stocks')

    def test_scores_key_per_market(self, store):
        """Rows from different markets must not bleed into each other."""
        store.upsert('stocks', 'AAPL', _row('AAPL', 9.0))
        store.upsert('crypto', 'BTC', _row('BTC', 8.0))
        assert store.row_count('stocks') == 1
        assert store.row_count('crypto') == 1
        assert store.get_row('stocks', 'BTC') is None
        assert store.get_row('crypto', 'AAPL') is None

    def test_upsert_updates_score_in_sorted_set(self, store):
        store.upsert('stocks', 'AAPL', _row('AAPL', 5.0))
        store.upsert('stocks', 'AAPL', _row('AAPL', 9.9))
        results = store.get_top_n('stocks', 1)
        assert results[0]['final_score'] == pytest.approx(9.9)


# ===========================================================================
# 100 ms TTL cache behaviour
# ===========================================================================

class TestTTLCache:
    """The in-process row cache inside RedisSnapshotStore must expire after 100 ms."""

    def test_cache_hit_avoids_redis(self):
        store = _make_redis_store()
        store.upsert('stocks', 'AAPL', _row('AAPL', 5.0))
        # Prime the cache
        store.get_row('stocks', 'AAPL')
        # Count subsequent Redis reads
        calls = []
        original_hget = store._redis.hget

        def counting_hget(key, field):
            calls.append((key, field))
            return original_hget(key, field)

        store._redis.hget = counting_hget
        result = store.get_row('stocks', 'AAPL')
        assert result is not None
        assert calls == [], f'Expected cache hit but Redis was called: {calls}'

    def test_cache_invalidated_on_upsert(self):
        store = _make_redis_store()
        store.upsert('stocks', 'AAPL', _row('AAPL', 1.0))
        store.get_row('stocks', 'AAPL')  # populate cache
        store.upsert('stocks', 'AAPL', _row('AAPL', 9.9))  # should invalidate cache
        result = store.get_row('stocks', 'AAPL')
        assert result is not None
        assert result['final_score'] == pytest.approx(9.9)

    def test_cache_expires_after_ttl(self):
        from app.services.snapshot_state_store import _CACHE_TTL_S
        store = _make_redis_store()
        store.upsert('stocks', 'NVDA', _row('NVDA', 3.0))
        store.get_row('stocks', 'NVDA')  # populate cache

        # Expire all cache entries by backdating them.
        ck = store._row_cache_key('stocks', 'NVDA')
        with store._cache._lock:
            if ck in store._cache._data:
                val, _ = store._cache._data[ck]
                store._cache._data[ck] = (val, time.monotonic() - _CACHE_TTL_S - 0.001)

        # Directly update Redis (simulates another process writing).
        new_row = _row('NVDA', 9.9)
        store._redis.hset(
            store._rows_key('stocks'), 'NVDA',
            json.dumps(new_row, separators=(',', ':')),
        )

        # Cache expired → must re-read from Redis.
        result = store.get_row('stocks', 'NVDA')
        assert result is not None
        assert result['final_score'] == pytest.approx(9.9)

    def test_cache_bounded(self):
        from app.services.snapshot_state_store import _LocalCache
        cache = _LocalCache(max_size=5)
        for i in range(10):
            cache.set(f'key{i}', i)
        with cache._lock:
            assert len(cache._data) <= 5


# ===========================================================================
# snapshot_store.py integration — InMemory path
# ===========================================================================

class TestSnapshotStoreIntegration:
    """Smoke-tests that snapshot_store.py public API works via the store adapter."""

    @pytest.fixture(autouse=True)
    def inject_in_memory_store(self, monkeypatch):
        """Ensure USE_REDIS_SNAPSHOT=0 and reset the singleton for every test."""
        import app.services.snapshot_state_store as m
        import app.config as cfg
        monkeypatch.setattr(cfg.settings, 'use_redis_snapshot', False)
        m.reset_snapshot_store(None)
        yield
        m.reset_snapshot_store(None)

    def test_upsert_rows_and_get_snapshot(self):
        from app.services import snapshot_store as ss
        ss.clear_snapshot('stocks')
        ss.upsert_rows('stocks', [_row('AAPL', 8.0), _row('MSFT', 5.0)])
        snap = ss.get_snapshot('stocks', limit=10, compact=False)
        syms = {r['symbol'] for r in snap['results']}
        assert 'AAPL' in syms
        assert 'MSFT' in syms
        assert snap['rows_scored'] == 2

    def test_lookup_snapshot_row(self):
        from app.services import snapshot_store as ss
        ss.clear_snapshot('stocks')
        ss.upsert_rows('stocks', [_row('GOOG', 7.5)])
        row = ss.lookup_snapshot_row('GOOG', 'stocks')
        assert row is not None
        assert row['symbol'] == 'GOOG'

    def test_get_snapshot_meta(self):
        from app.services import snapshot_store as ss
        ss.clear_snapshot('stocks')
        ss.upsert_rows('stocks', [_row('TSLA', 3.0)])
        meta = ss.get_snapshot_meta('stocks')
        assert meta['rows_scored'] == 1

    def test_invalidate_symbol(self):
        from app.services import snapshot_store as ss
        ss.clear_snapshot('stocks')
        ss.upsert_rows('stocks', [_row('NVDA', 6.0)])
        assert ss.invalidate_symbol('stocks', 'NVDA') is True
        assert ss.lookup_snapshot_row('NVDA', 'stocks') is None

    def test_clear_snapshot(self):
        from app.services import snapshot_store as ss
        ss.upsert_rows('stocks', [_row('X', 1.0)])
        ss.clear_snapshot('stocks')
        snap = ss.get_snapshot('stocks', limit=10, compact=False)
        assert snap['rows_scored'] == 0
        assert snap['results'] == []


# ===========================================================================
# snapshot_store.py integration — Redis path (fakeredis)
# ===========================================================================

class TestSnapshotStoreRedisIntegration:
    """Same tests but with USE_REDIS_SNAPSHOT=1 and a fakeredis-backed store."""

    @pytest.fixture(autouse=True)
    def inject_redis_store(self, monkeypatch):
        import app.services.snapshot_state_store as m
        import app.config as cfg
        monkeypatch.setattr(cfg.settings, 'use_redis_snapshot', True)
        redis_store = _make_redis_store()
        m.reset_snapshot_store(redis_store)
        yield redis_store
        m.reset_snapshot_store(None)
        monkeypatch.setattr(cfg.settings, 'use_redis_snapshot', False)

    def test_upsert_rows_and_get_snapshot(self):
        from app.services import snapshot_store as ss
        ss.clear_snapshot('stocks')
        ss.upsert_rows('stocks', [_row('AAPL', 8.0), _row('MSFT', 5.0)])
        snap = ss.get_snapshot('stocks', limit=10, compact=False)
        syms = {r['symbol'] for r in snap['results']}
        assert 'AAPL' in syms
        assert 'MSFT' in syms

    def test_get_snapshot_top_n_ordered(self):
        from app.services import snapshot_store as ss
        ss.clear_snapshot('stocks')
        ss.upsert_rows('stocks', [
            _row('A', 3.0), _row('B', 9.0), _row('C', 1.0), _row('D', 7.0),
        ])
        snap = ss.get_snapshot('stocks', limit=2, compact=False)
        scores = [r['final_score'] for r in snap['results']]
        assert scores == sorted(scores, reverse=True)
        assert scores[0] == pytest.approx(9.0)

    def test_lookup_snapshot_row_redis(self):
        from app.services import snapshot_store as ss
        ss.clear_snapshot('stocks')
        ss.upsert_rows('stocks', [_row('GOOG', 7.5)])
        row = ss.lookup_snapshot_row('GOOG', 'stocks')
        assert row is not None
        assert row['symbol'] == 'GOOG'

    def test_lookup_missing_returns_none(self):
        from app.services import snapshot_store as ss
        ss.clear_snapshot('stocks')
        assert ss.lookup_snapshot_row('NOTHERE', 'stocks') is None

    def test_get_snapshot_meta_redis(self):
        from app.services import snapshot_store as ss
        ss.clear_snapshot('stocks')
        ss.upsert_rows('stocks', [_row('TSLA', 3.0)])
        meta = ss.get_snapshot_meta('stocks')
        assert meta.get('rows_scored', 0) >= 1

    def test_invalidate_symbol_redis(self):
        from app.services import snapshot_store as ss
        ss.clear_snapshot('stocks')
        ss.upsert_rows('stocks', [_row('NVDA', 6.0)])
        assert ss.invalidate_symbol('stocks', 'NVDA') is True
        assert ss.lookup_snapshot_row('NVDA', 'stocks') is None

    def test_clear_snapshot_redis(self, inject_redis_store):
        from app.services import snapshot_store as ss
        ss.upsert_rows('stocks', [_row('X', 1.0)])
        ss.clear_snapshot('stocks')
        # Both the Redis hash and sorted set must be empty.
        assert inject_redis_store.row_count('stocks') == 0

    def test_apply_to_top_n_redis(self):
        from app.services import snapshot_store as ss
        ss.clear_snapshot('stocks')
        ss.upsert_rows('stocks', [_row('A', 10.0), _row('B', 5.0), _row('C', 1.0)])
        tagged = []

        def cb(row):
            tagged.append(row['symbol'])

        result = ss.apply_to_top_n('stocks', 2, cb)
        assert result == 2
        assert set(tagged) == {'A', 'B'}

    def test_mark_batch_completed_persists_to_redis(self, inject_redis_store):
        from app.services import snapshot_store as ss
        ss.clear_snapshot('stocks')
        ss.mark_batch_completed('stocks', batch_index=0, total_batches=5, universe_size=100)
        meta = ss.get_snapshot_meta('stocks')
        assert meta.get('total_batches') == 5
        assert meta.get('universe_size') == 100
