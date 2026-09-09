"""Thread-safe, process-local cache for compatible scanner artifacts.

The store holds no raw dictionaries for callers to mutate. It never runs a
loader, performs I/O, or waits for a refresh while its main lock is held.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from app.config import settings

MODE_DISABLED = "disabled"
MODE_SHADOW_WRITE = "shadow_write"
MODE_SHADOW_COMPARE = "shadow_compare"
MODE_ENABLED = "enabled"
VALID_MODES = {MODE_DISABLED, MODE_SHADOW_WRITE, MODE_SHADOW_COMPARE, MODE_ENABLED}

PRIORITY_TIER_1 = 0
PRIORITY_TIER_2 = 1
PRIORITY_NORMAL = 2
PRIORITY_TIER_3 = 3
DERIVED_DOMAINS = {
    "score_components", "composite_scores", "options_chains",
    "narratives", "bayesian_priors",
}


@dataclass
class CacheEntry:
    value: Any
    created_at_monotonic: float
    expires_at_monotonic: float
    fingerprint: str | None
    provider_id: str | None
    source_timestamp: float | None
    priority: int
    estimated_bytes: int
    symbol: str | None
    data_version: str
    cache_namespace: str
    last_access_monotonic: float | None = None
    provenance: dict[str, Any] | None = None


def stable_fingerprint(value: Any) -> str:
    """Return a stable digest for JSON-compatible material inputs."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class MemoryStore:
    """Bounded LRU cache with deterministic keys and per-key single-flight."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ):
        self._clock = clock
        self._random_uniform = random_uniform
        self._lock = threading.RLock()
        self._domains: dict[str, OrderedDict[str, CacheEntry]] = defaultdict(OrderedDict)
        self._flights: dict[tuple[str, str], threading.Event] = {}
        self._approx_bytes = 0
        self._last_prune_at: float | None = None
        self._emergency_mode = False
        self._stats: dict[str, dict[str, int]] = defaultdict(self._new_stats)

    @staticmethod
    def _new_stats() -> dict[str, int]:
        return {
            "hits": 0, "misses": 0, "expired": 0, "evictions": 0,
            "invalidations": 0, "singleflight_owners": 0,
            "singleflight_waits": 0, "singleflight_timeouts": 0,
            "singleflight_cancellations": 0,
            "correctness_samples": 0, "correctness_mismatches": 0,
        }

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        return (symbol or "").strip().upper()

    @staticmethod
    def _mode(domain: str | None = None) -> str:
        global_mode = settings.memory_cache_mode
        if global_mode != MODE_ENABLED:
            mode = global_mode
        else:
            mode = getattr(settings, f"cache_mode_{domain}", global_mode) if domain else global_mode
        return mode if mode in VALID_MODES else MODE_DISABLED

    @staticmethod
    def _domain_enabled(domain: str) -> bool:
        return bool(getattr(settings, f"cache_enable_{domain}", True))

    def _key(self, domain: str, dimensions: dict[str, Any]) -> str:
        normalized = dict(dimensions)
        if "symbol" in normalized:
            normalized["symbol"] = self.normalize_symbol(str(normalized["symbol"]))
        return stable_fingerprint(
            {"namespace": settings.memory_cache_namespace, "domain": domain, "dimensions": normalized}
        )

    def _ttl(self, ttl_seconds: float) -> float:
        jitter = max(0.0, min(1.0, settings.cache_ttl_jitter_percent))
        return max(0.0, ttl_seconds * (1.0 + self._random_uniform(-jitter, jitter)))

    def _read_enabled(self, domain: str) -> bool:
        return settings.memory_cache_enabled and self._domain_enabled(domain) and self._mode(domain) == MODE_ENABLED

    def _write_enabled(self, domain: str) -> bool:
        return settings.memory_cache_enabled and self._domain_enabled(domain) and self._mode(domain) != MODE_DISABLED

    def should_sample_correctness(self, domain: str) -> bool:
        """Return whether a cache hit should be baseline-recomputed by its caller."""
        rate = max(0.0, min(1.0, settings.cache_correctness_sample_rate))
        if not self._read_enabled(domain) or rate == 0.0:
            return False
        selected = self._random_uniform(0.0, 1.0) < rate
        if selected:
            with self._lock:
                self._stats[domain]["correctness_samples"] += 1
        return selected

    def record_correctness_comparison(self, domain: str, matches: bool) -> None:
        """Record a caller's sampled exact or tolerance-based comparison."""
        if not matches:
            with self._lock:
                self._stats[domain]["correctness_mismatches"] += 1

    def get(
        self,
        domain: str,
        dimensions: dict[str, Any],
        *,
        fingerprint: str | None = None,
        provider_id: str | None = None,
    ) -> Any | None:
        if not self._read_enabled(domain):
            return None
        key = self._key(domain, dimensions)
        now = self._clock()
        with self._lock:
            entries = self._domains[domain]
            entry = entries.get(key)
            if entry is None:
                self._stats[domain]["misses"] += 1
                return None
            incompatible = (
                now >= entry.expires_at_monotonic
                or entry.cache_namespace != settings.memory_cache_namespace
                or (fingerprint is not None and entry.fingerprint != fingerprint)
                or (provider_id is not None and entry.provider_id != provider_id)
            )
            if incompatible:
                entries.pop(key)
                self._approx_bytes -= entry.estimated_bytes
                self._stats[domain]["expired"] += 1
                self._stats[domain]["misses"] += 1
                return None
            entries.move_to_end(key)
            entry.last_access_monotonic = now
            self._stats[domain]["hits"] += 1
            return self._copy_for_consumer(domain, entry.value)

    def age_seconds(
        self,
        domain: str,
        dimensions: dict[str, Any],
        *,
        fingerprint: str | None = None,
        provider_id: str | None = None,
    ) -> float | None:
        """Return a compatible entry's age without exposing its payload."""
        if not self._read_enabled(domain):
            return None
        key = self._key(domain, dimensions)
        now = self._clock()
        with self._lock:
            entry = self._domains[domain].get(key)
            if entry is None:
                return None
            incompatible = (
                now >= entry.expires_at_monotonic
                or entry.cache_namespace != settings.memory_cache_namespace
                or (fingerprint is not None and entry.fingerprint != fingerprint)
                or (provider_id is not None and entry.provider_id != provider_id)
            )
            if incompatible:
                self._domains[domain].pop(key, None)
                self._approx_bytes -= entry.estimated_bytes
                self._stats[domain]["expired"] += 1
                return None
            return max(0.0, now - entry.created_at_monotonic)

    @staticmethod
    def _copy_for_storage(domain: str, value: Any) -> Any:
        """Keep daily-history frames canonical; pandas CoW isolates consumers."""
        if domain == "daily_history" and isinstance(value, pd.DataFrame):
            return value.copy(deep=False)
        return copy.deepcopy(value)

    @staticmethod
    def _copy_for_consumer(domain: str, value: Any) -> Any:
        if domain == "daily_history" and isinstance(value, pd.DataFrame):
            return value.copy(deep=False)
        return copy.deepcopy(value)

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
        priority: int = PRIORITY_NORMAL,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        if not self._write_enabled(domain):
            return
        key = self._key(domain, dimensions)
        now = self._clock()
        estimated_bytes = self._estimate_bytes(value)
        from app.services.cache_contract import sanitize_provenance

        entry = CacheEntry(
            value=self._copy_for_storage(domain, value),
            created_at_monotonic=now,
            expires_at_monotonic=now + self._ttl(ttl_seconds),
            fingerprint=fingerprint,
            provider_id=provider_id,
            source_timestamp=source_timestamp,
            priority=priority,
            estimated_bytes=estimated_bytes,
            symbol=self.normalize_symbol(str(dimensions.get("symbol", ""))) or None,
            data_version="v1",
            cache_namespace=settings.memory_cache_namespace,
            last_access_monotonic=now,
            provenance=sanitize_provenance(provenance),
        )
        with self._lock:
            entries = self._domains[domain]
            previous = entries.pop(key, None)
            if previous:
                self._approx_bytes -= previous.estimated_bytes
            entries[key] = entry
            self._approx_bytes += entry.estimated_bytes
            while len(entries) > settings.memory_cache_max_entries_per_domain:
                _, evicted = entries.popitem(last=False)
                self._approx_bytes -= evicted.estimated_bytes
                self._stats[domain]["evictions"] += 1
            self._enforce_budget_locked()

    @staticmethod
    def _estimate_bytes(value: Any) -> int:
        """Estimate tabular values without serializing a full DataFrame."""
        try:
            memory_usage = getattr(value, "memory_usage", None)
            if callable(memory_usage):
                usage = memory_usage(index=True, deep=True)
                return int(usage.sum() if hasattr(usage, "sum") else usage)
            if isinstance(value, (bytes, bytearray, str)):
                return len(value)
            return len(json.dumps(value, default=str).encode("utf-8"))
        except (AttributeError, TypeError, ValueError, OverflowError):
            return 0

    def get_or_compute(
        self,
        domain: str,
        dimensions: dict[str, Any],
        loader: Callable[[], Any],
        *,
        ttl_seconds: float,
        fingerprint: str | None = None,
        provider_id: str | None = None,
        source_timestamp: float | None = None,
        priority: int = PRIORITY_NORMAL,
        wait_timeout_seconds: float | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> Any:
        """Return a compatible entry or calculate it once per process/key."""
        cached = self.get(domain, dimensions, fingerprint=fingerprint, provider_id=provider_id)
        if cached is not None:
            return cached
        if not self._write_enabled(domain):
            return loader()

        key = self._key(domain, dimensions)
        flight_key = (domain, key)
        with self._lock:
            event = self._flights.get(flight_key)
            if event is None:
                event = threading.Event()
                self._flights[flight_key] = event
                owner = True
                self._stats[domain]["singleflight_owners"] += 1
            else:
                owner = False
                self._stats[domain]["singleflight_waits"] += 1

        if not owner:
            timeout = wait_timeout_seconds if wait_timeout_seconds is not None else settings.memory_cache_singleflight_timeout_seconds
            if event.wait(max(0.0, timeout)):
                cached = self.get(domain, dimensions, fingerprint=fingerprint, provider_id=provider_id)
                if cached is not None:
                    return cached
            else:
                with self._lock:
                    self._stats[domain]["singleflight_timeouts"] += 1
            return loader()

        try:
            value = loader()
            self.set(
                domain, dimensions, value, ttl_seconds=ttl_seconds, fingerprint=fingerprint,
                provider_id=provider_id, source_timestamp=source_timestamp, priority=priority,
                provenance=provenance,
            )
            return value
        except BaseException:
            # Cancellation and process-shutdown exceptions must not strand
            # waiters behind an event whose owner will never publish a value.
            with self._lock:
                self._stats[domain]["singleflight_cancellations"] += 1
            raise
        finally:
            with self._lock:
                self._flights.pop(flight_key, None)
                event.set()

    def get_quote(self, symbol: str, *, provider_id: str) -> Any | None:
        return self.get("quotes", {"symbol": symbol, "provider_id": provider_id}, provider_id=provider_id)

    def set_quote(self, symbol: str, quote: Any, *, provider_id: str, ttl_seconds: float, source_timestamp: float | None = None, priority: int = PRIORITY_NORMAL) -> None:
        self.set("quotes", {"symbol": symbol, "provider_id": provider_id}, quote, ttl_seconds=ttl_seconds, provider_id=provider_id, source_timestamp=source_timestamp, priority=priority, provenance={"provider_id": provider_id, "source_timestamp": source_timestamp})

    def get_daily_history(self, dimensions: dict[str, Any], *, fingerprint: str | None = None, provider_id: str | None = None) -> Any | None:
        from app.services.cache_contract import missing_dimensions
        if missing_dimensions("daily_history", dimensions):
            return None
        return self.get("daily_history", dimensions, fingerprint=fingerprint, provider_id=provider_id)

    def set_daily_history(self, dimensions: dict[str, Any], history: Any, *, ttl_seconds: float, fingerprint: str | None = None, provider_id: str | None = None, source_timestamp: float | None = None, priority: int = PRIORITY_NORMAL) -> bool:
        from app.services.cache_contract import missing_dimensions
        if missing_dimensions("daily_history", dimensions):
            return False
        self.set("daily_history", dimensions, history, ttl_seconds=ttl_seconds, fingerprint=fingerprint, provider_id=provider_id, source_timestamp=source_timestamp, priority=priority, provenance={"provider_id": provider_id, "source_timestamp": source_timestamp, "interval": dimensions.get("interval"), "adjustment_mode": dimensions.get("adjustment_mode")})
        return True

    def invalidate_symbol(self, symbol: str, domains: set[str] | None = None, provider_id: str | None = None) -> int:
        target = self.normalize_symbol(symbol)
        removed = 0
        with self._lock:
            for domain, entries in self._domains.items():
                if domains and domain not in domains:
                    continue
                for key, entry in list(entries.items()):
                    if entry.symbol == target and (provider_id is None or entry.provider_id == provider_id):
                        entries.pop(key)
                        self._approx_bytes -= entry.estimated_bytes
                        self._stats[domain]["invalidations"] += 1
                        removed += 1
        return removed

    def invalidate_provider(self, provider_id: str) -> int:
        """Remove entries belonging to a provider after its identity changes.

        Provider identifiers may include a generation suffix (``yahoo@2``).
        Matching the stable prefix makes a session reset safe without making
        callers discover every old generation.
        """
        target = (provider_id or "").strip().lower()
        if not target:
            return 0
        removed = 0
        with self._lock:
            for domain, entries in self._domains.items():
                for key, entry in list(entries.items()):
                    entry_provider = (entry.provider_id or "").lower()
                    if entry_provider == target or entry_provider.startswith(f"{target}@"):
                        entries.pop(key)
                        self._approx_bytes -= entry.estimated_bytes
                        self._stats[domain]["invalidations"] += 1
                        removed += 1
        return removed

    def reprioritize_symbol(
        self,
        symbol: str,
        priority: int,
        *,
        domains: set[str] | None = None,
    ) -> int:
        """Update eviction priority without changing otherwise-valid payloads."""
        target = self.normalize_symbol(symbol)
        updated = 0
        with self._lock:
            for domain, entries in self._domains.items():
                if domains is not None and domain not in domains:
                    continue
                for entry in entries.values():
                    if entry.symbol == target:
                        entry.priority = priority
                        updated += 1
            self._enforce_budget_locked()
        return updated

    def invalidate_domain(self, domain: str) -> int:
        with self._lock:
            entries = self._domains[domain]
            removed = len(entries)
            self._approx_bytes -= sum(entry.estimated_bytes for entry in entries.values())
            entries.clear()
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
                        self._approx_bytes -= entry.estimated_bytes
                        self._stats[domain]["expired"] += 1
                        removed += 1
            self._last_prune_at = now
            self._enforce_budget_locked()
        return removed

    def _enforce_budget_locked(self) -> None:
        budget = settings.memory_cache_max_mb * 1024 * 1024
        emergency_threshold = max(0.0, min(1.0, settings.memory_cache_emergency_percent))
        self._emergency_mode = bool(budget and self._approx_bytes >= budget * emergency_threshold)
        while self._approx_bytes > budget:
            candidates = [
                (entry.priority, entry.last_access_monotonic or entry.created_at_monotonic, domain, key, entry)
                for domain, entries in self._domains.items()
                for key, entry in entries.items()
            ]
            if not candidates:
                return
            # Tier 3 derived values are cheapest to recreate and must leave
            # first during pressure.  Other Tier 3 data is next, then normal
            # LRU/priority behavior preserves active scanner inputs.
            _, _, domain, key, entry = max(
                candidates,
                key=lambda item: (
                    item[0],
                    item[0] == PRIORITY_TIER_3 and item[2] in DERIVED_DOMAINS,
                    -item[1],
                ),
            )
            self._domains[domain].pop(key, None)
            self._approx_bytes -= entry.estimated_bytes
            self._stats[domain]["evictions"] += 1
        self._emergency_mode = bool(budget and self._approx_bytes >= budget * emergency_threshold)

    def relieve_emergency_pressure(self) -> int:
        """Evict Tier 3 derived artifacts down to the configured recovery mark."""
        budget = settings.memory_cache_max_mb * 1024 * 1024
        target = int(budget * max(0.0, min(1.0, settings.memory_cache_emergency_recovery_percent)))
        removed = 0
        with self._lock:
            candidates = [
                (entry.last_access_monotonic or entry.created_at_monotonic, domain, key, entry)
                for domain, entries in self._domains.items()
                for key, entry in entries.items()
                if entry.priority == PRIORITY_TIER_3 and domain in DERIVED_DOMAINS
            ]
            for _, domain, key, entry in sorted(candidates):
                if self._approx_bytes <= target:
                    break
                self._domains[domain].pop(key, None)
                self._approx_bytes -= entry.estimated_bytes
                self._stats[domain]["evictions"] += 1
                removed += 1
            self._enforce_budget_locked()
        return removed

    def get_stats(self) -> dict[str, Any]:
        from app.services.deployment_topology import cache_memory_budget, cache_topology

        with self._lock:
            domains = {}
            for domain, entries in self._domains.items():
                stat = dict(self._stats[domain])
                attempts = stat["hits"] + stat["misses"]
                domains[domain] = {
                    "entries": len(entries),
                    "approx_memory_bytes": sum(entry.estimated_bytes for entry in entries.values()),
                    **stat,
                    "hit_rate": round(stat["hits"] / attempts, 4) if attempts else 0.0,
                    "enabled": self._domain_enabled(domain),
                    "mode": self._mode(domain),
                }
            return {
                "enabled": settings.memory_cache_enabled,
                "mode": self._mode(),
                "namespace": settings.memory_cache_namespace,
                "process_scope": "local",
                "approx_memory_bytes": self._approx_bytes,
                "memory_budget_bytes": settings.memory_cache_max_mb * 1024 * 1024,
                "emergency_mode": self._emergency_mode,
                "topology": cache_topology(),
                "memory_budget": cache_memory_budget(),
                "last_prune_at_monotonic": self._last_prune_at,
                "domains": domains,
            }


memory_store = MemoryStore()
