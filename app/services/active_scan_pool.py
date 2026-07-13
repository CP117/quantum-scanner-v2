"""
In-memory rolling pool of "active scan" symbols (the top names worth the
extra cost of a real options-chain pull).

Maintained by `result_store` after each successful batch.  Read by
`score_symbol_rows` to decide which rows get `use_real_options=True`.
"""
from __future__ import annotations

import os
from threading import Lock
from time import time

_lock = Lock()
_pool: dict[str, float] = {}  # symbol -> last_seen_monotonic
_MAX_SIZE = 60
_EXPIRY_SECONDS = 60 * 30  # 30 min retention

# Phase 26.16 / Tier 2.4: throttle pool refreshes.
#
# Before: every successful batch (~once per 5s during steady-state sweep)
# triggered a re-sort + dict-update of the top-25 names. That work is
# cheap individually but the pool is read on EVERY scoring call to decide
# real_options usage, so the refresh ends up serialized behind every
# scorer's `active_scan_symbols()` lock acquire.
#
# After: refreshes are gated to once per MRD_ACTIVE_POOL_REFRESH_SECONDS
# (default 10s). The first batch in any window does the work; subsequent
# batches within the window are no-ops on the pool-update path. The pool
# entries already have their own 30-minute TTL so freshness is preserved.
_POOL_REFRESH_INTERVAL = max(0.0, float(os.environ.get('MRD_ACTIVE_POOL_REFRESH_SECONDS', '10')))
_last_refresh_at: float = 0.0


def active_scan_symbols() -> list[str]:
    """Return the current active-scan symbols, pruning stale entries."""
    now = time()
    with _lock:
        to_drop = [s for s, ts in _pool.items() if now - ts > _EXPIRY_SECONDS]
        for s in to_drop:
            _pool.pop(s, None)
        return list(_pool.keys())


def update_pool_from_rows(rows: list[dict], top_n: int = 25) -> bool:
    """Refresh the pool with the top-N symbols of the latest batch.

    Symbols are scored by `final_score` descending.  We do NOT clear the pool
    on every call - we just refresh the last-seen timestamp for the current
    top-N so previously-hot symbols stay warm if they keep ranking.

    Phase 26.16 / Tier 2.4: gated by `MRD_ACTIVE_POOL_REFRESH_SECONDS`.
    Returns True when a refresh actually happened, False when it was
    short-circuited by the interval gate.
    """
    global _last_refresh_at
    if not rows:
        return False
    now = time()
    if _POOL_REFRESH_INTERVAL > 0 and (now - _last_refresh_at) < _POOL_REFRESH_INTERVAL:
        # Inside the cooldown window — skip the sort + dict update entirely.
        return False
    try:
        ranked = sorted(
            (r for r in rows if r.get('symbol')),
            key=lambda r: float(r.get('final_score') or 0),
            reverse=True,
        )[:top_n]
        with _lock:
            _last_refresh_at = now
            for r in ranked:
                sym = r.get('symbol')
                if not sym:
                    continue
                _pool[sym] = now
            # Enforce size cap (keep most-recent)
            if len(_pool) > _MAX_SIZE:
                pruned = sorted(_pool.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_SIZE]
                _pool.clear()
                _pool.update(dict(pruned))
        return True
    except Exception:
        return False


def pool_stats() -> dict:
    with _lock:
        return {
            'size': len(_pool),
            'cap': _MAX_SIZE,
            'expiry_seconds': _EXPIRY_SECONDS,
            # Phase 26.16 / Tier 2.4 telemetry. Cast to int because the
            # /system/status pydantic schema declares dict[str, int].
            'refresh_interval_seconds': int(_POOL_REFRESH_INTERVAL),
            'last_refresh_at_monotonic': int(_last_refresh_at),
        }


def reset_pool() -> None:
    global _last_refresh_at
    with _lock:
        _pool.clear()
        _last_refresh_at = 0.0
