"""Process-local, bounded cache for deterministic scanner artifacts.

Entries are isolated by namespace and source dimensions.  The store never
performs I/O while locked; callers compute misses outside the store.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any

from app.config import settings


@dataclass
class CacheEntry:
    value: Any
    created_at_monotonic: float
    expires_at_monotonic: float
    fingerprint: str | None = None
    source_timestamp: float | None = None
    last_access_monotonic: float | None = None
    estimated_bytes: int = 0
    provider_id: str | None = None
    data_version: str | None = None
    cache_namespace: str | None = None
    priority: int = 2
    symbol: str | None = None


def stable_fingerprint(value: Any) -> str:
    """Return a process-independent digest for JSON-compatible inputs."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class MemoryStore:
    """Thread-safe LRU caches with approximate per-process memory budgeting."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._domains: dict[str, OrderedDict[str, CacheEntry]] = defaultdict(OrderedDict)
        self._stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {"hits": 0, "misses": 0, "expired": 0, "evictions": 0, "invalidations": 0}
        )
        self._last_prune_at: float | None = None

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        return (symbol or "").strip().upper()

    def _key(self, domain: str, dimensions: dict[str, Any]) -> str:
        normalized = dict(dimensions)
        if "symbol" in normalized:
            normalized["symbol"] = self.normalize_symbol(str(normalized["symbol"]))
        return stable_fingerprint({"namespace": settings.memory_cache_namespace, "domain": domain, "key": normalized})

    def get(
        self,
        domain: str,
        dimensions: dict[str, Any],
        *,
        fingerprint: str | None = None,
        provider_id: str | None = None,
        copy_value: bool = True,
    ) -> Any | None:
        if not settings.memory_cache_enabled or settings.memory_cache_mode != "enabled":
            return None
        key = self._key(domain, dimensions)
        now = self._clock()
        with self._lock:
            entries = self._domains[domain]
            entry = entries.get(key)
            if entry is None:
                self._stats[domain]["misses"] += 1
                return None
            if (
                now >= entry.expires_at_monotonic
                or entry.cache_namespace != settings.memory_cache_namespace
                or (fingerprint is not None and entry.fingerprint != fingerprint)
                or (provider_id is not None and entry.provider_id != provider_id)
            ):
                entries.pop(key, None)
                self._stats[domain]["expired"] += 1
                self._stats[domain]["misses"] += 1
                return None
            entries.move_to_end(key)
            entry.last_access_monotonic = now
            self._stats[domain]["hits"] += 1
            return copy.deepcopy(entry.value) if copy_value else entry.value

    def set(
        self,
        domain: str,
        dimensions: dict[str, Any],
        value: Any,
        *,
        ttl_seconds: float,
        fingerprint: str | None = None,
        provider_id: str | None = None,
        source_timestamp: float | None = None,
        priority: int = 2,
    ) -> None:
        if not settings.memory_cache_enabled or settings.memory_cache_mode == "disabled":
            return
        key = self._key(domain, dimensions)
        now = self._clock()
        try:
            estimated_bytes = len(json.dumps(value, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            estimated_bytes = 0
        with self._lock:
            entries = self._domains[domain]
            entries[key] = CacheEntry(
                value=copy.deepcopy(value),
                created_at_monotonic=now,
                expires_at_monotonic=now + max(0.0, ttl_seconds),
                fingerprint=fingerprint,
                source_timestamp=source_timestamp,
                last_access_monotonic=now,
                estimated_bytes=estimated_bytes,
                provider_id=provider_id,
                data_version="v1",
                cache_namespace=settings.memory_cache_namespace,
                priority=priority,
                symbol=self.normalize_symbol(str(dimensions.get("symbol", ""))) or None,
            )
            entries.move_to_end(key)
            while len(entries) > settings.memory_cache_max_entries_per_domain:
                entries.popitem(last=False)
                self._stats[domain]["evictions"] += 1
            self._enforce_budget_locked()

    def invalidate_symbol(self, symbol: str, domains: set[str] | None = None) -> int:
        target = self.normalize_symbol(symbol)
        removed = 0
        with self._lock:
            for domain, entries in self._domains.items():
                if domains and domain not in domains:
                    continue
                for key, entry in list(entries.items()):
                    if entry.symbol == target:
                        entries.pop(key)
                        self._stats[domain]["invalidations"] += 1
                        removed += 1
        return removed

    def invalidate_domain(self, domain: str) -> int:
        with self._lock:
            removed = len(self._domains[domain])
            self._domains[domain].clear()
            self._stats[domain]["invalidations"] += removed
            return removed

    def prune_expired(self) -> int:
        now = self._clock()
        removed = 0
        with self._lock:
            for domain, entries in self._domains.items():
                for key, entry in list(entries.items()):
                    if now >= entry.expires_at_monotonic:
                        entries.pop(key)
                        self._stats[domain]["expired"] += 1
                        removed += 1
            self._last_prune_at = now
            self._enforce_budget_locked()
        return removed

    def _enforce_budget_locked(self) -> None:
        budget = settings.memory_cache_max_mb * 1024 * 1024
        total = sum(entry.estimated_bytes for entries in self._domains.values() for entry in entries.values())
        while total > budget:
            candidates = [(entry.priority, entry.last_access_monotonic or entry.created_at_monotonic, domain, key, entry)
                          for domain, entries in self._domains.items() for key, entry in entries.items()]
            if not candidates:
                return
            _, _, domain, key, entry = min(candidates)
            self._domains[domain].pop(key, None)
            self._stats[domain]["evictions"] += 1
            total -= entry.estimated_bytes

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            domains = {}
            total = 0
            for domain, entries in self._domains.items():
                bytes_used = sum(entry.estimated_bytes for entry in entries.values())
                total += bytes_used
                stat = dict(self._stats[domain])
                attempts = stat["hits"] + stat["misses"]
                domains[domain] = {
                    "entries": len(entries), "approx_memory_bytes": bytes_used,
                    **stat, "hit_rate": round(stat["hits"] / attempts, 4) if attempts else 0.0,
                }
            return {
                "enabled": settings.memory_cache_enabled,
                "mode": settings.memory_cache_mode,
                "namespace": settings.memory_cache_namespace,
                "process_scope": "local",
                "approx_memory_bytes": total,
                "memory_budget_bytes": settings.memory_cache_max_mb * 1024 * 1024,
                "last_prune_at_monotonic": self._last_prune_at,
                "domains": domains,
            }


memory_store = MemoryStore()
