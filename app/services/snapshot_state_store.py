"""
Snapshot State Store — Phase B
================================

Pluggable adapter for snapshot-store read/write state:
  - ``InMemorySnapshotStore`` — backward-compatible default (single-process).
  - ``RedisSnapshotStore``    — multi-process, Redis-backed with 100 ms
                                read-through cache to limit round-trips in
                                hot loops.

Selection is controlled by ``USE_REDIS_SNAPSHOT=0/1`` (default ``0``).

Redis layout
-------------
For each *market* (``stocks`` / ``crypto``):
  ``{prefix}:{market}:rows``    — Hash, field=SYMBOL, value=JSON row.
  ``{prefix}:{market}:meta``    — Hash, field=meta-key, value=JSON scalar.
  ``{prefix}:{market}:scores``  — Sorted Set, score=final_score, member=SYMBOL.
                                  Enables O(log N) top-N retrieval.

The ``_LocalCache`` implementation is shared with the tier state store via an
explicit import so that both stores reuse the same bounded, TTL-aware cache
class without duplicating it.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger('app.snapshot_state_store')

# ---------------------------------------------------------------------------
# Re-use the LocalCache from Phase A
# ---------------------------------------------------------------------------

_CACHE_TTL_S = 0.100  # 100 ms hot-path read-through cache TTL


class _LocalCache:
    """Bounded, TTL-expiring dict cache.

    Entries expire individually after ``_CACHE_TTL_S`` seconds.  The cache
    never grows beyond ``max_size`` entries; when full, the oldest entry is
    evicted to keep memory bounded.
    """

    def __init__(self, max_size: int = 20_000) -> None:
        self._data: Dict[str, tuple] = {}  # key → (value, expires_at)
        self._max = max_size
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            val, exp = entry
            if time.monotonic() >= exp:
                del self._data[key]
                return None
            return val

    def set(self, key: str, value) -> None:
        with self._lock:
            if key not in self._data and len(self._data) >= self._max:
                oldest = next(iter(self._data))
                del self._data[oldest]
            self._data[key] = (value, time.monotonic() + _CACHE_TTL_S)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class SnapshotStore(ABC):
    """Thin adapter exposing exactly the call sites used by snapshot_store."""

    @abstractmethod
    def upsert(self, market: str, symbol: str, row: dict) -> None:
        """Upsert a scored row for *symbol* in *market*."""

    @abstractmethod
    def get_top_n(self, market: str, limit: int) -> List[dict]:
        """Return up to *limit* rows sorted by final_score descending."""

    @abstractmethod
    def get_row(self, market: str, symbol: str) -> Optional[dict]:
        """Return the full row for *symbol* or None."""

    @abstractmethod
    def delete_row(self, market: str, symbol: str) -> bool:
        """Delete *symbol* from *market*.  Returns True if it existed."""

    @abstractmethod
    def clear(self, market: str) -> None:
        """Remove all rows and meta for *market*."""

    @abstractmethod
    def row_count(self, market: str) -> int:
        """Return the number of stored rows for *market*."""

    @abstractmethod
    def get_meta(self, market: str) -> Dict[str, Any]:
        """Return the meta dict for *market*."""

    @abstractmethod
    def set_meta(self, market: str, updates: Dict[str, Any]) -> None:
        """Merge *updates* into the meta dict for *market*."""

    @abstractmethod
    def apply_to_top_n(
        self,
        market: str,
        top_n: int,
        callback: Callable[[dict], None],
    ) -> int:
        """Run *callback* on each of the top-N rows; return the count applied."""

    # ------------------------------------------------------------------
    # Convenience helpers with default implementations
    # ------------------------------------------------------------------

    def is_empty(self, market: str) -> bool:
        return self.row_count(market) == 0

    def all_symbols(self, market: str) -> List[str]:
        """Return all stored symbols for *market* (no ordering guarantee)."""
        return [r.get('symbol', '') for r in self.get_top_n(market, 999_999)]


# ---------------------------------------------------------------------------
# In-memory implementation (default, backward-compatible)
# ---------------------------------------------------------------------------

class InMemorySnapshotStore(SnapshotStore):
    """Pure in-process dict store — wraps the existing module-level dicts.

    When *snapshot_dict* and *meta_dict* are passed in, the store operates
    directly on those objects (same behaviour as before Phase B).  This
    avoids copying any data and means external callers that still hold a
    reference to the original dicts see the same mutations.
    """

    def __init__(
        self,
        snapshot_dict: Optional[Dict[str, Dict[str, dict]]] = None,
        meta_dict: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        # Lazily import the module-level dicts only when the store is
        # actually constructed so unit tests can substitute their own.
        if snapshot_dict is None or meta_dict is None:
            from app.services import snapshot_store as _ss
            snapshot_dict = snapshot_dict if snapshot_dict is not None else _ss._snapshot
            meta_dict = meta_dict if meta_dict is not None else _ss._snapshot_meta
        self._snap: Dict[str, Dict[str, dict]] = snapshot_dict
        self._meta: Dict[str, Dict[str, Any]] = meta_dict

    # ------------------------------------------------------------------
    # SnapshotStore interface
    # ------------------------------------------------------------------

    def upsert(self, market: str, symbol: str, row: dict) -> None:
        self._snap.setdefault(market, {})[symbol] = row

    def get_top_n(self, market: str, limit: int) -> List[dict]:
        bucket = self._snap.get(market, {})
        rows = sorted(bucket.values(), key=lambda r: -(r.get('final_score') or 0))
        return rows[:limit]

    def get_row(self, market: str, symbol: str) -> Optional[dict]:
        bucket = self._snap.get(market, {})
        row = bucket.get(symbol)
        if row is None:
            return None
        return dict(row)

    def delete_row(self, market: str, symbol: str) -> bool:
        bucket = self._snap.get(market, {})
        if symbol in bucket:
            del bucket[symbol]
            return True
        return False

    def clear(self, market: str) -> None:
        self._snap[market] = {}
        self._meta[market] = dict(self._meta.get(market, {}))
        self._meta[market]['rows_scored'] = 0
        self._meta[market]['current_batch_index'] = 0

    def row_count(self, market: str) -> int:
        return len(self._snap.get(market, {}))

    def get_meta(self, market: str) -> Dict[str, Any]:
        return dict(self._meta.get(market, {}))

    def set_meta(self, market: str, updates: Dict[str, Any]) -> None:
        self._meta.setdefault(market, {}).update(updates)

    def apply_to_top_n(
        self,
        market: str,
        top_n: int,
        callback: Callable[[dict], None],
    ) -> int:
        bucket = self._snap.get(market, {})
        if not bucket:
            return 0
        ordered = sorted(
            bucket.values(),
            key=lambda r: float(r.get('final_score') or 0),
            reverse=True,
        )[:top_n]
        applied = 0
        for row in ordered:
            try:
                callback(row)
                row['_snapshot_refreshed_at'] = time.monotonic()
                applied += 1
            except Exception as exc:  # noqa: BLE001
                log.debug('InMemorySnapshotStore.apply_to_top_n callback failed for %s: %s',
                          row.get('symbol'), exc)
        return applied

    def all_symbols(self, market: str) -> List[str]:
        return list(self._snap.get(market, {}).keys())


# ---------------------------------------------------------------------------
# Redis implementation
# ---------------------------------------------------------------------------

class RedisSnapshotStore(SnapshotStore):
    """Redis-backed snapshot store with a 100 ms in-process read-through cache.

    Redis layout (per *market*)
    ----------------------------
    ``{prefix}:{market}:rows``   — Hash, field=SYMBOL, value=JSON blob.
    ``{prefix}:{market}:meta``   — Hash, field=meta-key, value=JSON scalar.
    ``{prefix}:{market}:scores`` — Sorted Set, score=final_score, member=SYMBOL.
    """

    def __init__(
        self,
        host: str = 'localhost',
        port: int = 6379,
        db: int = 0,
        password: str = '',
        prefix: str = 'qs:snap',
    ) -> None:
        import redis  # imported lazily so InMemorySnapshotStore avoids the dep

        kwargs: dict = dict(host=host, port=port, db=db, decode_responses=True)
        if password:
            kwargs['password'] = password
        self._redis = redis.Redis(**kwargs)
        self._prefix = prefix
        self._cache = _LocalCache()

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _rows_key(self, market: str) -> str:
        return f'{self._prefix}:{market}:rows'

    def _meta_key(self, market: str) -> str:
        return f'{self._prefix}:{market}:meta'

    def _scores_key(self, market: str) -> str:
        return f'{self._prefix}:{market}:scores'

    def _row_cache_key(self, market: str, symbol: str) -> str:
        return f'r:{market}:{symbol}'

    def _meta_cache_key(self, market: str) -> str:
        return f'm:{market}'

    # ------------------------------------------------------------------
    # SnapshotStore interface
    # ------------------------------------------------------------------

    def upsert(self, market: str, symbol: str, row: dict) -> None:
        final_score = float(row.get('final_score') or 0.0)
        blob = json.dumps(row, separators=(',', ':'), default=str)
        pipe = self._redis.pipeline(transaction=False)
        pipe.hset(self._rows_key(market), symbol, blob)
        pipe.zadd(self._scores_key(market), {symbol: final_score})
        pipe.execute()
        # Invalidate local row + meta caches
        self._cache.invalidate(self._row_cache_key(market, symbol))
        self._cache.invalidate(self._meta_cache_key(market))

    def get_top_n(self, market: str, limit: int) -> List[dict]:
        # Fetch top-N symbols from Sorted Set (O(log N + limit)).
        symbols = self._redis.zrevrangebyscore(
            self._scores_key(market), '+inf', '-inf',
            start=0, num=limit,
        )
        if not symbols:
            return []
        # Batch-fetch rows from Hash (O(limit)).
        blobs = self._redis.hmget(self._rows_key(market), symbols)
        results: List[dict] = []
        for blob in blobs:
            if blob is None:
                continue
            try:
                results.append(json.loads(blob))
            except Exception:  # noqa: BLE001
                pass
        return results

    def get_row(self, market: str, symbol: str) -> Optional[dict]:
        ck = self._row_cache_key(market, symbol)
        cached = self._cache.get(ck)
        if cached is not None:
            return dict(cached)
        blob = self._redis.hget(self._rows_key(market), symbol)
        if blob is None:
            return None
        try:
            row = json.loads(blob)
        except Exception:  # noqa: BLE001
            return None
        self._cache.set(ck, row)
        return dict(row)

    def delete_row(self, market: str, symbol: str) -> bool:
        pipe = self._redis.pipeline(transaction=False)
        pipe.hdel(self._rows_key(market), symbol)
        pipe.zrem(self._scores_key(market), symbol)
        results = pipe.execute()
        self._cache.invalidate(self._row_cache_key(market, symbol))
        self._cache.invalidate(self._meta_cache_key(market))
        return bool(results[0])

    def clear(self, market: str) -> None:
        pipe = self._redis.pipeline(transaction=False)
        pipe.delete(self._rows_key(market))
        pipe.delete(self._scores_key(market))
        pipe.delete(self._meta_key(market))
        pipe.execute()
        self._cache.invalidate(self._meta_cache_key(market))

    def row_count(self, market: str) -> int:
        return self._redis.hlen(self._rows_key(market))

    def get_meta(self, market: str) -> Dict[str, Any]:
        ck = self._meta_cache_key(market)
        cached = self._cache.get(ck)
        if cached is not None:
            return dict(cached)
        raw = self._redis.hgetall(self._meta_key(market))
        meta: Dict[str, Any] = {}
        for k, v in raw.items():
            try:
                meta[k] = json.loads(v)
            except Exception:  # noqa: BLE001
                meta[k] = v
        self._cache.set(ck, meta)
        return dict(meta)

    def set_meta(self, market: str, updates: Dict[str, Any]) -> None:
        if not updates:
            return
        mapping = {k: json.dumps(v, default=str) for k, v in updates.items()}
        self._redis.hset(self._meta_key(market), mapping=mapping)
        self._cache.invalidate(self._meta_cache_key(market))

    def apply_to_top_n(
        self,
        market: str,
        top_n: int,
        callback: Callable[[dict], None],
    ) -> int:
        if top_n <= 0:
            return 0
        symbols = self._redis.zrevrangebyscore(
            self._scores_key(market), '+inf', '-inf',
            start=0, num=top_n,
        )
        if not symbols:
            return 0
        blobs = self._redis.hmget(self._rows_key(market), symbols)
        applied = 0
        pipe = self._redis.pipeline(transaction=False)
        for sym, blob in zip(symbols, blobs):
            if blob is None:
                continue
            try:
                row = json.loads(blob)
            except Exception:  # noqa: BLE001
                continue
            try:
                callback(row)
                row['_snapshot_refreshed_at'] = time.monotonic()
                new_blob = json.dumps(row, separators=(',', ':'), default=str)
                pipe.hset(self._rows_key(market), sym, new_blob)
                self._cache.invalidate(self._row_cache_key(market, sym))
                applied += 1
            except Exception as exc:  # noqa: BLE001
                log.debug('RedisSnapshotStore.apply_to_top_n callback failed for %s: %s',
                          sym, exc)
        if applied:
            pipe.execute()
        return applied

    def all_symbols(self, market: str) -> List[str]:
        return self._redis.hkeys(self._rows_key(market))


# ---------------------------------------------------------------------------
# Factory / singleton
# ---------------------------------------------------------------------------

_store: Optional[SnapshotStore] = None
_store_lock = threading.Lock()


def get_snapshot_store() -> SnapshotStore:
    """Return the process-wide SnapshotStore singleton.

    Created on first call; subsequent calls return the cached instance.
    """
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        from app.config import settings
        if settings.use_redis_snapshot:
            log.info(
                'snapshot_state_store: USE_REDIS_SNAPSHOT=1 — initialising RedisSnapshotStore '
                '(%s:%d db=%d prefix=%s)',
                settings.redis_host, settings.redis_port,
                settings.redis_db, settings.redis_snapshot_prefix,
            )
            kwargs: dict = dict(
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
                prefix=settings.redis_snapshot_prefix,
            )
            if settings.redis_password:
                kwargs['password'] = settings.redis_password
            _store = RedisSnapshotStore(**kwargs)
        else:
            log.info('snapshot_state_store: USE_REDIS_SNAPSHOT=0 — using InMemorySnapshotStore')
            _store = InMemorySnapshotStore()
    return _store


def reset_snapshot_store(store: Optional[SnapshotStore] = None) -> None:
    """Replace the singleton (used in tests to inject a mock store)."""
    global _store
    with _store_lock:
        _store = store
