"""
Tier 1 Active Scanner — Phase 28
==================================

Rescores the top-100 (Tier 1) symbols every 2-4 seconds with full precision.

Design mirrors the existing ``top10_priority_service`` but:
  * Operates on `tier_manager.get_tier_symbols(TIER_1)` instead of the fixed
    snapshot top-N.
  * Uses ``force_full_pass2=True`` so every row gets the full extended-factor
    pipeline regardless of its Pass-1 rank.
  * Reports heartbeats to ``tier_resilience`` so the watchdog knows it's alive.
  * On each cycle, calls ``tier_manager.rebalance()`` to keep assignments
    fresh (the rebalance itself is lightweight — it only inspects in-memory
    scores).

Thread model
------------
A single long-lived daemon thread.  Self-healing: exceptions inside the loop
body are caught, logged, and reported as tier errors.  The thread never exits.
"""
from __future__ import annotations

import logging
import threading
import time

from app.services import tier_manager, tier_resilience
from app.services.tier_manager import TIER_1

log = logging.getLogger('app.tier1_scanner')

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()

# Telemetry
_last_tick_at: float = 0.0
_total_ticks: int = 0
_total_rescored: int = 0


def _tier1_loop(interval_seconds: float) -> None:
    """Main Tier 1 scan loop (daemon thread body)."""
    global _last_tick_at, _total_ticks, _total_rescored

    # Lazy imports — keeps this module cheap to import.
    from app.services.snapshot_store import get_snapshot, upsert_rows
    from app.services.scoring_service import score_symbol_rows
    from app.services.universe_service import load_universe

    log.info('tier1_scanner: started (interval=%.1fs)', interval_seconds)

    # Build a symbol → seed-row lookup for enriching scored rows.
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
                        '_tier': TIER_1,
                    }
    except Exception:  # noqa: BLE001
        log.warning('tier1_scanner: failed to prime universe metadata', exc_info=True)

    # Adaptive back-off (mirrors top10_priority_service logic).
    consecutive_slow = 0
    monitor_only = False

    while not _stop_event.is_set():
        cycle_start = time.monotonic()
        try:
            if not monitor_only:
                _run_tick(universe_meta, upsert_rows, score_symbol_rows, get_snapshot)
        except Exception as exc:  # noqa: BLE001
            log.warning('tier1_scanner: tick error: %s', exc)
            tier_resilience.record_error(1, str(exc))

        elapsed = time.monotonic() - cycle_start
        _last_tick_at = time.time()
        _total_ticks += 1
        tier_resilience.heartbeat(1)

        # Adaptive back-off.
        if elapsed > 5.0:
            consecutive_slow += 1
            if consecutive_slow >= 3:
                monitor_only = True
                log.warning('tier1_scanner: entering monitor-only mode (slow ticks)')
        else:
            if monitor_only and elapsed < 2.0:
                monitor_only = False
                consecutive_slow = 0
                log.info('tier1_scanner: exiting monitor-only mode')
            else:
                consecutive_slow = max(0, consecutive_slow - 1)

        sleep_time = interval_seconds * (5.0 if monitor_only else 1.0)
        time.sleep(max(0.1, sleep_time - elapsed))


def _run_tick(
    universe_meta: dict,
    upsert_rows,
    score_symbol_rows,
    get_snapshot,
) -> None:
    """One scan cycle: score all Tier 1 symbols, upsert into snapshot, rebalance."""
    global _total_rescored

    # Get current Tier 1 symbol list from tier_manager.
    tier1_syms = tier_manager.get_tier_symbols(TIER_1)
    if not tier1_syms:
        # Fallback: use the current snapshot top-100 if tier_manager hasn't
        # assigned anything yet.
        snap = get_snapshot('stocks', limit=100, compact=True)
        tier1_syms = [r.get('symbol') for r in (snap.get('results') or []) if r.get('symbol')]

    if not tier1_syms:
        return

    # Build seed rows.
    seed_rows = []
    for sym in tier1_syms:
        seed = universe_meta.get(sym.upper()) or {'symbol': sym.upper(), '_tier': TIER_1}
        seed_rows.append(dict(seed))

    # Full-depth scoring with force_full_pass2=True.
    scored = score_symbol_rows(seed_rows, force_full_pass2=True)
    if scored:
        # Tag rows as Tier 1 and update composite scores in tier_manager.
        for row in scored:
            sym = (row.get('symbol') or '').upper()
            if sym:
                row['_tier'] = TIER_1
                tier_manager.update_composite_score(sym, float(row.get('final_score') or 0))
        # Upsert into the snapshot store (Tier 1 rows are always in the visible bucket).
        upsert_rows('stocks', scored)
        _total_rescored += len(scored)

    # Trigger a tier rebalance so promotions/demotions happen promptly.
    try:
        tier_manager.rebalance('stocks')
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Start / stop
# ---------------------------------------------------------------------------

def start_tier1_scanner(interval_seconds: float | None = None) -> bool:
    """Start the Tier 1 scanner thread.  Returns True if newly started.

    *interval_seconds* defaults to ``settings.tier_1_interval_seconds``.
    Idempotent — subsequent calls are no-ops.
    """
    global _thread
    import sys
    in_pytest = ('PYTEST_CURRENT_TEST' in __import__('os').environ or 'pytest' in sys.modules)
    if in_pytest and __import__('os').environ.get('TIER1_FORCE_START') != '1':
        log.info('tier1_scanner: skipped under pytest')
        return False

    if interval_seconds is None:
        try:
            from app.config import settings
            interval_seconds = settings.tier_1_interval_seconds
        except Exception:  # noqa: BLE001
            interval_seconds = 3.0

    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop_event.clear()
        _thread = threading.Thread(
            target=_tier1_loop,
            args=(interval_seconds,),
            name='tier1-scanner',
            daemon=True,
        )
        _thread.start()
    log.info('tier1_scanner: thread launched (interval=%.1fs)', interval_seconds)
    return True


def stop_tier1_scanner(timeout: float = 5.0) -> None:
    """Signal the Tier 1 scanner to stop and wait for *timeout* seconds."""
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=timeout)


def get_status() -> dict:
    """Return Tier 1 scanner telemetry."""
    return {
        'tier': 1,
        'running': _thread is not None and _thread.is_alive(),
        'last_tick_at': _last_tick_at or None,
        'total_ticks': _total_ticks,
        'total_rescored': _total_rescored,
        'tier_1_symbols': tier_manager.get_tier_symbols(TIER_1),
        'health': tier_resilience.get_tier_health().get('1', {}),
    }
