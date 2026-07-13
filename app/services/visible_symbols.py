"""Phase 26.51 — Visible-symbols registry for viewport-driven priority.

The frontend periodically POSTs the symbols currently on the user's
first page (after all filters / sort are applied) to
`POST /api/future_mode/visible_symbols`.  The priority lane reads this
registry on every tick and ALWAYS includes those symbols in its
re-score + GARCH-overlay pass, in addition to the global top-N by
final_score.

This guarantees that whatever the user is looking at right now is
under continuous deep-scan — every horizon, every tier, every signal —
regardless of where it sits in the global ranking.

Thread-safe (single Lock).  TTL-bounded so a closed tab eventually
stops getting prioritized; tab activity refreshes the TTL on every
ping.
"""
from __future__ import annotations

import threading
import time
from typing import Iterable

# Default TTL: a visible-symbols ping is "alive" for 90 s.  Frontend
# pings every 5 s while visible, so any reasonable connectivity blip
# is well within tolerance.  Past the TTL the symbol drops back to
# whatever the global priority lane decides.
_TTL_SECONDS = 90.0

# Maximum number of symbols we accept per market.  Bounded to prevent
# a runaway tab from forcing the priority lane to chew through
# thousands of symbols per cycle.  100 is comfortably above any
# reasonable first-page size.
_MAX_PER_MARKET = 100

_lock = threading.Lock()
# market -> { symbol: expires_at_monotonic }
_registry: dict[str, dict[str, float]] = {'stocks': {}, 'crypto': {}}


def register_visible(market: str, symbols: Iterable[str], ttl_s: float | None = None) -> int:
    """Record that the given symbols are currently visible to the user
    in `market`.  Returns the number of symbols accepted into the
    registry after dedupe + TTL refresh.
    """
    mkt = (market or 'stocks').lower()
    if mkt not in _registry:
        mkt = 'stocks'
    ttl = float(ttl_s if ttl_s is not None else _TTL_SECONDS)
    expires_at = time.monotonic() + ttl
    # Normalise + dedupe input.
    seen: set[str] = set()
    cleaned: list[str] = []
    for s in symbols:
        if not s:
            continue
        sym = str(s).strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        cleaned.append(sym)
        if len(cleaned) >= _MAX_PER_MARKET:
            break
    with _lock:
        bucket = _registry.setdefault(mkt, {})
        # Purge expired entries while we're holding the lock.
        now_mono = time.monotonic()
        stale = [k for k, exp in bucket.items() if exp < now_mono]
        for k in stale:
            bucket.pop(k, None)
        # Register / refresh.
        for sym in cleaned:
            bucket[sym] = expires_at
        return len(cleaned)


def get_visible(market: str = 'stocks') -> list[str]:
    """Return the set of currently-visible (un-expired) symbols for
    `market`, in insertion (= push) order."""
    mkt = (market or 'stocks').lower()
    now_mono = time.monotonic()
    with _lock:
        bucket = _registry.get(mkt) or {}
        # Filter out expired
        live = [(sym, exp) for sym, exp in bucket.items() if exp >= now_mono]
        # Same lock — purge in place.
        for k, exp in list(bucket.items()):
            if exp < now_mono:
                bucket.pop(k, None)
    # Stable order by expiry desc (most-recent pings first).
    live.sort(key=lambda kv: kv[1], reverse=True)
    return [sym for sym, _ in live]


def clear(market: str | None = None) -> None:
    """Drop the registry for `market` (or all markets if None).
    Mostly used by tests."""
    with _lock:
        if market is None:
            for k in list(_registry.keys()):
                _registry[k] = {}
        else:
            _registry[(market or 'stocks').lower()] = {}


def get_stats() -> dict:
    """Telemetry helper — surfaced by the cache-metrics endpoint."""
    now_mono = time.monotonic()
    with _lock:
        out: dict = {}
        for mkt, bucket in _registry.items():
            live = sum(1 for exp in bucket.values() if exp >= now_mono)
            out[mkt] = {
                'tracked': len(bucket),
                'live': live,
                'ttl_seconds': int(_TTL_SECONDS),
                'cap_per_market': _MAX_PER_MARKET,
            }
        return out
