"""
Tier 2 Monitor Scanner — Phase 28
===================================

Rescores the next 1,000 (Tier 2) symbols every 30-60 seconds using the
lightweight ``score_depth='cheap'`` path.

Responsibilities
----------------
  * Pull the current Tier 2 symbol list from ``tier_manager``.
  * Score in batches using ``score_symbol_rows`` (cheap pass only — no full
    extended factors).
  * Cache scored rows in ``tier_cache_store`` (Tier 2 in-memory LRU).
  * Report scores back to ``tier_manager`` so rebalance can promote/demote.
  * Send a heartbeat to ``tier_resilience`` each tick.
  * Symbols whose scores jump above the Tier 1 threshold are promoted on the
    next ``rebalance()`` call.

Batching
--------
Tier 2 symbols are scored in chunks of 50 to keep provider pressure low
(Tier 2 has a 20 % provider budget).  The total cycle time at 1 K symbols is
approximately: (1000/50) × 3 s/batch ≈ 60 s, which matches the 30-60 s target.
"""
from __future__ import annotations

import logging
import threading
import time

from app.services import tier_manager, tier_resilience
from app.services.tier_manager import TIER_2

log = logging.getLogger('app.tier2_scanner')

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()

_T2_BATCH_SIZE = int(__import__('os').environ.get('TIER2_BATCH_SIZE', '50'))

# Telemetry
_last_tick_at: float = 0.0
_total_ticks: int = 0
_total_rescored: int = 0


def _tier2_loop(interval_seconds: float) -> None:
    """Main Tier 2 scan loop (daemon thread body)."""
    global _last_tick_at, _total_ticks, _total_rescored

    from app.services.snapshot_store import upsert_rows
    from app.services.scoring_service import score_symbol_rows
    from app.services.universe_service import load_universe
    from app.services.tier_cache_store import save_tier2_row

    log.info('tier2_scanner: started (interval=%.1fs)', interval_seconds)

    universe_meta: dict[str, dict] = {}
    try:
        for market in ('stocks', 'crypto'):
            for u in load_universe(market):
                sym = (u.get('symbol') or '').upper()
                if sym:
                    universe_meta[sym] = {
                        'symbol': sym,
                        'name': u.get('name', ''),
                        'exchange': u.get('exchange', ''),
                        '_tier': TIER_2,
                    }
    except Exception:  # noqa: BLE001
        log.warning('tier2_scanner: failed to prime universe metadata', exc_info=True)

    while not _stop_event.is_set():
        cycle_start = time.monotonic()
        try:
            tier2_syms = tier_manager.get_tier_symbols(TIER_2)
            if tier2_syms:
                rescored = _score_batch(
                    tier2_syms, universe_meta, score_symbol_rows,
                    upsert_rows, save_tier2_row,
                )
                _total_rescored += rescored
        except Exception as exc:  # noqa: BLE001
            log.warning('tier2_scanner: tick error: %s', exc)
            tier_resilience.record_error(2, str(exc))

        _last_tick_at = time.time()
        _total_ticks += 1
        tier_resilience.heartbeat(2)

        elapsed = time.monotonic() - cycle_start
        sleep_time = max(1.0, interval_seconds - elapsed)
        time.sleep(sleep_time)


def _score_batch(
    symbols: list[str],
    universe_meta: dict,
    score_symbol_rows,
    upsert_rows,
    save_tier2_row,
) -> int:
    """Score symbols in batches of _T2_BATCH_SIZE; return count scored."""
    rescored = 0
    for i in range(0, len(symbols), _T2_BATCH_SIZE):
        if _stop_event.is_set():
            break
        chunk = symbols[i: i + _T2_BATCH_SIZE]
        seed_rows = []
        for sym in chunk:
            seed = universe_meta.get(sym.upper()) or {'symbol': sym.upper(), '_tier': TIER_2}
            seed_rows.append(dict(seed))
        try:
            scored = score_symbol_rows(seed_rows)
            for row in scored:
                sym = (row.get('symbol') or '').upper()
                if sym:
                    row['_tier'] = TIER_2
                    tier_manager.update_composite_score(sym, float(row.get('final_score') or 0))
                    save_tier2_row(sym, row)
            # Tier 2 rows are not pushed to the main snapshot (virtual).
            # They are visible via the /api/scan/snapshot?tier=2 parameter
            # or when they're promoted to Tier 1 by the rebalancer.
            rescored += len(scored)
        except Exception as exc:  # noqa: BLE001
            log.debug('tier2_scanner: batch error (chunk %d): %s', i, exc)
            tier_resilience.record_error(2, str(exc))
        # Small sleep between batches to respect provider quota.
        time.sleep(0.2)
    return rescored


# ---------------------------------------------------------------------------
# Start / stop
# ---------------------------------------------------------------------------

def start_tier2_scanner(interval_seconds: float | None = None) -> bool:
    """Start the Tier 2 scanner thread.  Idempotent."""
    global _thread
    import sys, os
    in_pytest = ('PYTEST_CURRENT_TEST' in os.environ or 'pytest' in sys.modules)
    if in_pytest and os.environ.get('TIER2_FORCE_START') != '1':
        log.info('tier2_scanner: skipped under pytest')
        return False

    if interval_seconds is None:
        try:
            from app.config import settings
            interval_seconds = settings.tier_2_interval_seconds
        except Exception:  # noqa: BLE001
            interval_seconds = 45.0

    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop_event.clear()
        _thread = threading.Thread(
            target=_tier2_loop,
            args=(interval_seconds,),
            name='tier2-scanner',
            daemon=True,
        )
        _thread.start()
    log.info('tier2_scanner: thread launched (interval=%.1fs)', interval_seconds)
    return True


def stop_tier2_scanner(timeout: float = 5.0) -> None:
    """Signal the Tier 2 scanner to stop and wait for *timeout* seconds."""
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=timeout)


def get_status() -> dict:
    """Return Tier 2 scanner telemetry."""
    from app.services.tier_cache_store import get_tier2_size
    return {
        'tier': 2,
        'running': _thread is not None and _thread.is_alive(),
        'last_tick_at': _last_tick_at or None,
        'total_ticks': _total_ticks,
        'total_rescored': _total_rescored,
        'tier_2_symbol_count': len(tier_manager.get_tier_symbols(TIER_2)),
        'cache_size': get_tier2_size(),
        'health': tier_resilience.get_tier_health().get('2', {}),
    }
