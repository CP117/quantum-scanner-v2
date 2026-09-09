"""
Tier State Store — Phase A (Issue #12)
=======================================

Pluggable adapter for tier-manager read/write state:
  - ``InMemoryStateStore``  — backward-compatible default (single-process).
  - ``RedisStateStore``     — multi-process, Redis-backed with 100 ms read-
                              through cache to limit round-trips in hot loops.

Selection is controlled by ``USE_REDIS_STATE=0/1`` (default ``0``).

Bootstrap behaviour (Redis store)
----------------------------------
On first call to ``RedisStateStore.bootstrap_from_file(path)`` the store
checks whether Redis already contains tier-assignment data.  If empty, it
loads the JSON file and seeds Redis.  Subsequent calls (or process restarts)
skip the load because Redis already has state.  This prevents split-brain
between the on-disk snapshot and Redis.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger('app.tier_state_store')

# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class StateStore(ABC):
    """Thin adapter exposing exactly the call sites used by tier_manager."""

    @abstractmethod
    def get_tier(self, symbol: str) -> Optional[int]:
        """Return the stored tier (1/2/3) or None if unknown."""

    @abstractmethod
    def set_tier(self, symbol: str, tier: int) -> None:
        """Persist the tier assignment for *symbol*."""

    @abstractmethod
    def get_score(self, symbol: str) -> Optional[float]:
        """Return the stored composite score or None if unknown."""

    @abstractmethod
    def set_score(self, symbol: str, score: float) -> None:
        """Persist the composite score for *symbol*."""

    @abstractmethod
    def all_tiers(self) -> Dict[str, int]:
        """Return a snapshot dict {symbol: tier}."""

    @abstractmethod
    def all_scores(self) -> Dict[str, float]:
        """Return a snapshot dict {symbol: score}."""

    @abstractmethod
    def bulk_load(
        self,
        tiers: Dict[str, int],
        scores: Dict[str, float],
    ) -> None:
        """Bulk-import tier and score data (used during bootstrap)."""

    def is_empty(self) -> bool:
        """Return True when no tier assignments are stored yet."""
        return len(self.all_tiers()) == 0

    def delete_symbol(self, symbol: str) -> None:
        """Delete a removed-universe symbol (optional for custom stores)."""
        return None


# ---------------------------------------------------------------------------
# In-memory implementation (default, backward-compatible)
# ---------------------------------------------------------------------------

class InMemoryStateStore(StateStore):
    """Pure in-process dict store — same behaviour as the legacy module globals."""

    def __init__(self) -> None:
        self._tiers: Dict[str, int] = {}
        self._scores: Dict[str, float] = {}

    def get_tier(self, symbol: str) -> Optional[int]:
        return self._tiers.get(symbol)

    def set_tier(self, symbol: str, tier: int) -> None:
        self._tiers[symbol] = tier

    def get_score(self, symbol: str) -> Optional[float]:
        return self._scores.get(symbol)

    def set_score(self, symbol: str, score: float) -> None:
        self._scores[symbol] = score

    def all_tiers(self) -> Dict[str, int]:
        return dict(self._tiers)

    def all_scores(self) -> Dict[str, float]:
        return dict(self._scores)

    def bulk_load(self, tiers: Dict[str, int], scores: Dict[str, float]) -> None:
        self._tiers.update(tiers)
        self._scores.update(scores)

    def delete_symbol(self, symbol: str) -> None:
        self._tiers.pop(symbol, None)
        self._scores.pop(symbol, None)


# ---------------------------------------------------------------------------
# Redis implementation
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
                # Evict the first (oldest-inserted) entry.
                oldest = next(iter(self._data))
                del self._data[oldest]
            self._data[key] = (value, time.monotonic() + _CACHE_TTL_S)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


class RedisStateStore(StateStore):
    """Redis-backed state store with a 100 ms in-process read-through cache.

    Redis layout
    ------------
    ``{prefix}:tiers``   — Hash  field=SYMBOL  value=tier_int (string)
    ``{prefix}:scores``  — Hash  field=SYMBOL  value=score_float (string)
    """

    def __init__(
        self,
        host: str = 'localhost',
        port: int = 6379,
        db: int = 0,
        password: str = '',
        prefix: str = 'qs:tier',
    ) -> None:
        import redis  # imported lazily so InMemoryStateStore avoids the dep

        kwargs: dict = dict(host=host, port=port, db=db, decode_responses=True)
        if password:
            kwargs['password'] = password
        self._redis = redis.Redis(**kwargs)
        self._prefix = prefix
        self._tiers_key = f'{prefix}:tiers'
        self._scores_key = f'{prefix}:scores'
        self._cache = _LocalCache()

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _tier_cache_key(self, symbol: str) -> str:
        return f't:{symbol}'

    def _score_cache_key(self, symbol: str) -> str:
        return f's:{symbol}'

    # ------------------------------------------------------------------
    # StateStore interface
    # ------------------------------------------------------------------

    def get_tier(self, symbol: str) -> Optional[int]:
        ck = self._tier_cache_key(symbol)
        cached = self._cache.get(ck)
        if cached is not None:
            return cached
        raw = self._redis.hget(self._tiers_key, symbol)
        if raw is None:
            return None
        val = int(raw)
        self._cache.set(ck, val)
        return val

    def set_tier(self, symbol: str, tier: int) -> None:
        self._redis.hset(self._tiers_key, symbol, str(tier))
        self._cache.invalidate(self._tier_cache_key(symbol))

    def get_score(self, symbol: str) -> Optional[float]:
        ck = self._score_cache_key(symbol)
        cached = self._cache.get(ck)
        if cached is not None:
            return cached
        raw = self._redis.hget(self._scores_key, symbol)
        if raw is None:
            return None
        val = float(raw)
        self._cache.set(ck, val)
        return val

    def set_score(self, symbol: str, score: float) -> None:
        self._redis.hset(self._scores_key, symbol, str(score))
        self._cache.invalidate(self._score_cache_key(symbol))

    def all_tiers(self) -> Dict[str, int]:
        raw = self._redis.hgetall(self._tiers_key)
        return {k: int(v) for k, v in raw.items()}

    def all_scores(self) -> Dict[str, float]:
        raw = self._redis.hgetall(self._scores_key)
        return {k: float(v) for k, v in raw.items()}

    def bulk_load(self, tiers: Dict[str, int], scores: Dict[str, float]) -> None:
        pipe = self._redis.pipeline(transaction=False)
        if tiers:
            pipe.hset(self._tiers_key, mapping={k: str(v) for k, v in tiers.items()})
        if scores:
            pipe.hset(self._scores_key, mapping={k: str(v) for k, v in scores.items()})
        pipe.execute()

    def delete_symbol(self, symbol: str) -> None:
        self._redis.hdel(self._tiers_key, symbol)
        self._redis.hdel(self._scores_key, symbol)
        self._cache.invalidate(self._tier_cache_key(symbol))
        self._cache.invalidate(self._score_cache_key(symbol))

    def bootstrap_from_file(self, path: Path) -> None:
        """Seed Redis from *path* (tier_state.json) if Redis is currently empty.

        Logs the outcome so operators can confirm which code path ran.
        """
        if not self.is_empty():
            log.info(
                'tier_state_store: Redis already has tier state — skipping file bootstrap (%s)',
                path,
            )
            return
        if not path.exists():
            log.info(
                'tier_state_store: Redis empty but %s not found — starting fresh', path
            )
            return
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            tiers = {k: int(v) for k, v in (data.get('assignments') or {}).items()}
            scores = {k: float(v) for k, v in (data.get('scores') or {}).items()}
            self.bulk_load(tiers, scores)
            log.info(
                'tier_state_store: seeded Redis from %s (%d tiers, %d scores)',
                path, len(tiers), len(scores),
            )
        except Exception:
            log.warning(
                'tier_state_store: failed to bootstrap Redis from %s', path, exc_info=True
            )


# ---------------------------------------------------------------------------
# Factory / singleton
# ---------------------------------------------------------------------------

_store: Optional[StateStore] = None
_store_lock = threading.Lock()


def get_store() -> StateStore:
    """Return the process-wide StateStore singleton.

    Created on first call; subsequent calls return the cached instance.
    """
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is not None:
            return _store
        from app.config import settings
        if settings.use_redis_state:
            log.info(
                'tier_state_store: USE_REDIS_STATE=1 — initialising RedisStateStore '
                '(%s:%d db=%d prefix=%s)',
                settings.redis_host, settings.redis_port,
                settings.redis_db, settings.redis_tier_prefix,
            )
            _redis_kwargs: dict = dict(
                host=settings.redis_host,
                port=settings.redis_port,
                db=settings.redis_db,
                prefix=settings.redis_tier_prefix,
            )
            _auth = settings.redis_password
            if _auth:
                _redis_kwargs['password'] = _auth
            _store = RedisStateStore(**_redis_kwargs)
        else:
            log.info('tier_state_store: USE_REDIS_STATE=0 — using InMemoryStateStore')
            _store = InMemoryStateStore()
    return _store


def reset_store(store: Optional[StateStore] = None) -> None:
    """Replace the singleton (used in tests to inject a mock store)."""
    global _store
    with _store_lock:
        _store = store