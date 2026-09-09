"""Tier-aware cache lifecycle policy shared by scanners and maintenance.

This module deliberately performs no provider I/O.  A Tier 1 promotion queues
freshness work for the active scanner, while history remains reusable because
it is both expensive and valid at its own source/TTL contract.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

from app.services.memory_store import (
    DERIVED_DOMAINS,
    PRIORITY_TIER_1,
    PRIORITY_TIER_2,
    PRIORITY_TIER_3,
    memory_store,
)

_lock = threading.Lock()
_tier1_refreshes: OrderedDict[str, float] = OrderedDict()


def _priority(tier: int) -> int:
    return {
        1: PRIORITY_TIER_1,
        2: PRIORITY_TIER_2,
        3: PRIORITY_TIER_3,
    }.get(tier, PRIORITY_TIER_3)


def on_tier_change(symbol: str, previous_tier: int, new_tier: int) -> None:
    """Synchronize cache residency and queue Tier 1 freshness enforcement."""
    sym = memory_store.normalize_symbol(symbol)
    if not sym or previous_tier == new_tier:
        return
    if new_tier == 3:
        # Do not throw away valid daily history or quotes on a demotion.
        # Derived values are safe to recompute and become first eviction
        # candidates under pressure.
        memory_store.reprioritize_symbol(
            sym, PRIORITY_TIER_3, domains=DERIVED_DOMAINS,
        )
        return

    memory_store.reprioritize_symbol(sym, _priority(new_tier))
    if new_tier == 1:
        with _lock:
            _tier1_refreshes[sym] = time.monotonic()


def take_tier1_refresh_requests(limit: int | None = None) -> list[str]:
    """Drain bounded promotion refresh requests in FIFO order."""
    with _lock:
        count = len(_tier1_refreshes) if limit is None else max(0, limit)
        result = list(_tier1_refreshes.keys())[:count]
        for sym in result:
            _tier1_refreshes.pop(sym, None)
        return result


def low_priority_prefetch_halted() -> bool:
    """True when adding low-priority work would prolong memory pressure."""
    return bool(memory_store.get_stats().get("emergency_mode"))


def invalidate_symbol_artifacts(
    symbol: str,
    *,
    remove_tier_state: bool = False,
) -> int:
    """Invalidate every cache representation for a removed/blacklisted symbol."""
    sym = memory_store.normalize_symbol(symbol)
    if not sym:
        return 0
    removed = memory_store.invalidate_symbol(sym)
    with _lock:
        _tier1_refreshes.pop(sym, None)
    # Local durable caches have separate identities; clean those as well so
    # a removed symbol cannot be re-admitted by a disk fallback.
    try:
        from app.services.quote_cache import invalidate_quote
        removed += int(invalidate_quote(sym))
    except Exception:
        pass
    try:
        from app.services.daily_history_service import invalidate
        removed += int(invalidate(sym))
    except Exception:
        pass
    try:
        from app.services.options_chain_service import invalidate_cached_chains
        removed += int(invalidate_cached_chains(sym))
    except Exception:
        pass
    try:
        from app.services.tier_cache_store import invalidate_symbol
        removed += int(invalidate_symbol(sym))
    except Exception:
        pass
    if remove_tier_state:
        try:
            from app.services.tier_manager import remove_symbol
            remove_symbol(sym)
        except Exception:
            pass
    return removed


def relieve_emergency_pressure() -> dict[str, int]:
    """Run the bounded, Tier-3-first part of emergency maintenance."""
    expired = memory_store.prune_expired()
    evicted = memory_store.relieve_emergency_pressure()
    return {"expired": expired, "tier3_derived_evicted": evicted}


def status() -> dict[str, object]:
    with _lock:
        pending = len(_tier1_refreshes)
    return {
        "tier1_freshness_requests": pending,
        "low_priority_prefetch_halted": low_priority_prefetch_halted(),
    }
