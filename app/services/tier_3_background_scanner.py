"""
Tier 3 Background Scanner — Phase 28
======================================

Rescans all Tier 3 symbols (the remaining ~6,500-7,500) once per hour when
a GPU is available, or once per day on CPU-only rigs.

Goals
-----
  * Keep Tier 3 composite scores fresh enough that the promotion engine can
    identify emerging opportunities and graduate them to Tier 2/1.
  * Use the cheapest possible data path (EOD prices from the quote cache,
    cached daily history) so the scan doesn't consume provider quota.
  * Detect volume spikes and overnight price gaps and trigger immediate
    Tier 3 → Tier 2 promotions.
  * Write a lightweight summary for each symbol to ``tier_cache_store`` so
    subsequent score retrievals are cache-hits.

Scoring model
-------------
``gpu_acceleration.batch_compute_scores`` is used when a GPU is available.
On CPU-only machines, the same function falls back to NumPy, but the scan
interval is extended to once-daily to avoid pegging the CPU.

Adaptive chunking
-----------------
Tier 3 symbols are processed in chunks of *_T3_BATCH_SIZE* (default 200).
Between chunks the loop yields (``time.sleep(0.1)``) so other threads (Tier 1,
Tier 2, HTTP handlers) stay responsive.
"""
from __future__ import annotations

import logging
import threading
import time

from app.services import tier_manager, tier_resilience
from app.services.tier_manager import TIER_3

log = logging.getLogger('app.tier3_scanner')

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()

_T3_BATCH_SIZE = int(__import__('os').environ.get('TIER3_BATCH_SIZE', '200'))

# Telemetry
_last_full_pass_at: float = 0.0
_total_passes: int = 0
_total_rescored: int = 0
_last_pass_duration_s: float = 0.0


def _tier3_loop(interval_seconds: float) -> None:
    """Main Tier 3 scan loop (daemon thread body)."""
    global _last_full_pass_at, _total_passes, _total_rescored, _last_pass_duration_s

    from app.services.gpu_acceleration import GPU_AVAILABLE, batch_compute_scores
    from app.services.tier_cache_store import save_tier3_summary, get_tier3_summary
    from app.services.universe_service import load_universe
    from app.services.quote_cache import get_cached_quote, cached_quote_is_usable, quote_age_seconds

    log.info(
        'tier3_scanner: started (interval=%.0fs, GPU=%s)',
        interval_seconds, GPU_AVAILABLE,
    )

    while not _stop_event.is_set():
        pass_start = time.monotonic()
        try:
            _run_full_pass(
                load_universe, get_cached_quote, cached_quote_is_usable,
                quote_age_seconds, batch_compute_scores,
                save_tier3_summary, get_tier3_summary,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning('tier3_scanner: full-pass error: %s', exc)
            tier_resilience.record_error(3, str(exc))

        _last_full_pass_at = time.time()
        _total_passes += 1
        _last_pass_duration_s = time.monotonic() - pass_start
        tier_resilience.heartbeat(3)

        log.info(
            'tier3_scanner: pass %d complete — %.0f s, total_rescored=%d',
            _total_passes, _last_pass_duration_s, _total_rescored,
        )
        time.sleep(max(60.0, interval_seconds - _last_pass_duration_s))


def _run_full_pass(
    load_universe,
    get_cached_quote,
    cached_quote_is_usable,
    quote_age_seconds,
    batch_compute_scores,
    save_tier3_summary,
    get_tier3_summary,
) -> None:
    """Score every Tier 3 symbol and update tier_manager scores."""
    global _total_rescored

    tier3_syms = tier_manager.get_tier_symbols(TIER_3)
    if not tier3_syms:
        log.debug('tier3_scanner: no Tier 3 symbols to scan')
        return

    log.info('tier3_scanner: starting full pass — %d symbols', len(tier3_syms))

    for chunk_start in range(0, len(tier3_syms), _T3_BATCH_SIZE):
        if _stop_event.is_set():
            break
        chunk = tier3_syms[chunk_start: chunk_start + _T3_BATCH_SIZE]

        # Build lightweight row stubs from the quote cache (no live provider calls).
        rows: list[dict] = []
        for sym in chunk:
            cached = get_cached_quote(sym)
            stub = {'symbol': sym, '_tier': TIER_3}
            if cached and cached_quote_is_usable(sym):
                stub['final_score'] = float(cached.get('final_score') or 0)
                stub['change_pct'] = float(cached.get('change_pct') or 0)
                stub['last_price'] = float(cached.get('last_price') or 0)
                stub['previous_close'] = float(cached.get('previous_close') or 0)
                stub['averageVolume'] = float(cached.get('averageVolume') or 0)
                # Reuse existing factor_breakdown from cache if present.
                if cached.get('factor_breakdown'):
                    stub['factor_breakdown'] = cached['factor_breakdown']
            rows.append(stub)

        # Compute lightweight scores (GPU or NumPy).
        try:
            scores = batch_compute_scores(rows)
        except Exception as exc:  # noqa: BLE001
            log.debug('tier3_scanner: batch_compute_scores error: %s', exc)
            scores = [float(r.get('final_score') or 0) for r in rows]

        for row, score in zip(rows, scores):
            sym = (row.get('symbol') or '').upper()
            if not sym:
                continue
            tier_manager.update_composite_score(sym, score)

            # Event-driven promotions: volume spike or price gap.
            current_vol = float(row.get('volume') or 0)
            avg_vol = float(row.get('averageVolume') or 0)
            last_px = float(row.get('last_price') or 0)
            prev_close = float(row.get('previous_close') or 0)

            if current_vol > 0 and avg_vol > 0:
                tier_manager.check_volume_spike(sym, current_vol, avg_vol)
            if last_px > 0 and prev_close > 0:
                tier_manager.check_price_gap(sym, last_px, prev_close)

            # Persist a compact summary to the disk shard.
            summary = {
                'symbol': sym,
                'final_score': score,
                'change_pct': row.get('change_pct', 0),
                'last_price': row.get('last_price', 0),
                'previous_close': row.get('previous_close', 0),
                'tier': TIER_3,
            }
            try:
                save_tier3_summary(sym, summary)
            except Exception:  # noqa: BLE001
                pass

        _total_rescored += len(rows)

        # Brief yield so Tier 1/2 and HTTP threads stay responsive.
        time.sleep(0.1)

    # After a full pass, rebalance so score-driven promotions land promptly.
    try:
        result = tier_manager.rebalance('stocks')
        log.info('tier3_scanner: post-pass rebalance: %s', result)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Start / stop
# ---------------------------------------------------------------------------

def start_tier3_scanner(interval_seconds: float | None = None) -> bool:
    """Start the Tier 3 background scanner.  Idempotent."""
    global _thread
    import sys, os
    in_pytest = ('PYTEST_CURRENT_TEST' in os.environ or 'pytest' in sys.modules)
    if in_pytest and os.environ.get('TIER3_FORCE_START') != '1':
        log.info('tier3_scanner: skipped under pytest')
        return False

    if interval_seconds is None:
        try:
            from app.config import settings
            from app.services.gpu_acceleration import GPU_AVAILABLE
            interval_seconds = (
                settings.tier_3_interval_seconds
                if GPU_AVAILABLE
                else settings.tier_3_interval_no_gpu_seconds
            )
        except Exception:  # noqa: BLE001
            interval_seconds = 3600.0

    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop_event.clear()
        _thread = threading.Thread(
            target=_tier3_loop,
            args=(interval_seconds,),
            name='tier3-scanner',
            daemon=True,
        )
        _thread.start()
    log.info('tier3_scanner: thread launched (interval=%.0fs)', interval_seconds)
    return True


def stop_tier3_scanner(timeout: float = 10.0) -> None:
    """Signal the Tier 3 scanner to stop and wait for *timeout* seconds."""
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=timeout)


def get_status() -> dict:
    """Return Tier 3 scanner telemetry."""
    try:
        from app.services.gpu_acceleration import GPU_AVAILABLE
    except Exception:  # noqa: BLE001
        GPU_AVAILABLE = False
    return {
        'tier': 3,
        'running': _thread is not None and _thread.is_alive(),
        'last_full_pass_at': _last_full_pass_at or None,
        'total_passes': _total_passes,
        'total_rescored': _total_rescored,
        'last_pass_duration_seconds': round(_last_pass_duration_s, 1),
        'tier_3_symbol_count': len(tier_manager.get_tier_symbols(TIER_3)),
        'gpu_available': GPU_AVAILABLE,
        'health': tier_resilience.get_tier_health().get('3', {}),
    }
