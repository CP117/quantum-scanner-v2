"""
Market activity tracker — gates crypto-provider HTTP traffic.

Phase 25: when the user is only viewing the stock scanner, the
CoinGecko / CryptoCompare / CoinPaprika providers were still firing
on every snapshot sweep — burning HTTP bandwidth, contributing to
"Provider busy / degraded" status events, and pulling thread pool
budget away from stock scoring.

This tiny service tracks the last-used wall-clock time per market.
Activity is "stamped" whenever:
  - The user requests `/api/scan/snapshot?market=crypto`
  - The user opens a crypto symbol detail (`/stock/{sym}?market=crypto`)
  - The user triggers a crypto refresh or prediction
  - The user runs a crypto backtest

When the most-recent activity for `crypto` is older than the
ACTIVITY_TTL_SECONDS window (default 10 min), the crypto scan loop
flips into CACHE-ONLY mode: it keeps walking the universe (so the
ranking eventually catches up when the user re-engages), but no
provider HTTP calls are issued.  The stock scanner gets the full
network budget.

Stocks are always considered active (they're the default tab).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

# 10 minutes of post-activity grace before crypto goes back to cache-only.
ACTIVITY_TTL_SECONDS = float(os.environ.get('MARKET_ACTIVITY_TTL', '600'))

_lock = threading.RLock()
_last_active: dict[str, float] = {
    'stocks': 0.0,  # stocks always treated as active; this just for symmetry
    'crypto': 0.0,
}


def stamp_active(market: Optional[str]) -> None:
    """Record that a market was just touched by the user."""
    if not market:
        return
    m = market.lower()
    if m not in _last_active:
        return
    with _lock:
        _last_active[m] = time.monotonic()


def is_active(market: Optional[str]) -> bool:
    """Was the market touched within the TTL window?

    Stocks are ALWAYS active (the default view).  Crypto only when
    the user has interacted with it recently.
    """
    if not market:
        return True
    m = market.lower()
    if m == 'stocks':
        return True
    if m not in _last_active:
        return True
    with _lock:
        last = _last_active.get(m, 0.0)
    return last > 0 and (time.monotonic() - last) <= ACTIVITY_TTL_SECONDS


def seconds_since_active(market: str) -> float:
    """Diagnostic helper for the providers page."""
    m = (market or '').lower()
    with _lock:
        last = _last_active.get(m, 0.0)
    if last <= 0:
        return float('inf')
    return time.monotonic() - last


def status() -> dict:
    """Snapshot of activity timestamps for /system/status + provider page."""
    out = {}
    with _lock:
        for m, last in _last_active.items():
            age = (time.monotonic() - last) if last > 0 else None
            out[m] = {
                'last_seen_seconds_ago': round(age, 1) if age is not None else None,
                'is_active': m == 'stocks' or (age is not None and age <= ACTIVITY_TTL_SECONDS),
            }
    out['activity_ttl_seconds'] = ACTIVITY_TTL_SECONDS
    return out
