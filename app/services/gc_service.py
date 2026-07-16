"""
Garbage-collector tuning + per-sweep checkpoint hook.

Phase 26.33 (CPU win for long-haul scans):
  The user reports CPU spikes from 10% → 100% after the first universal
  pass, with resident memory staying flat (~41% RSS) — the classic
  signature of GC pressure on a large heap rather than a true leak.

  Why this happens:
    - Pass 1 inflates the heap from ~50 MB → ~400-600 MB as snapshot
      rows, history caches, regulatory state, and provider telemetry
      get populated.  Most of those objects survive into gen-2.
    - Python's default thresholds `(700, 10, 10)` fire gen-0 every 700
      allocations and gen-2 every ~70k allocations.  At our allocation
      rate (~50k objects per batch during scoring), gen-2 collections
      walk the entire 400-600 MB heap mid-batch every ~1-2 batches,
      stealing 200-500 ms of CPU each time.
    - Worse, gen-2 is unpredictable: a batch starts at 10% CPU and
      ends at 100% if it happened to trip a full collection.  That's
      the bouncing-CPU pattern.

  What we do:
    1. **Tune thresholds** to (10_000, 20, 10) on startup.  Gen-0 still
       runs frequently (cheap, cleans short-lived garbage) but gen-2
       fires ~10x less often during scoring.
    2. **Force a `gc.collect()` at sweep boundaries** (every ~12,000
       symbols).  This batches the expensive full collection into a
       known idle moment (right after the snapshot wraps) instead of
       letting it interrupt scoring.  The cost moves from "random
       100% spikes during batches" to "one predictable 500 ms pause
       between passes."
    3. **Expose telemetry** so `/system/status.gc_stats` shows how
       many sweep-boundary collections have happened and how long the
       last one took — operator visibility into the optimization.

  Net effect: pass-2+ scoring CPU drops back down to pass-1 levels
  because the heap isn't being repeatedly walked during the hot path.
"""
from __future__ import annotations

import gc
import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

_TUNED = False
_TUNE_LOCK = threading.Lock()

# Default thresholds we install on startup.  Chosen so that gen-0
# remains cheap and frequent (~10k allocations) but gen-1/gen-2 fire
# much less often during the scoring hot path.  Gen-2 is what walks
# the entire heap and is what we want to defer.
_TUNED_THRESHOLDS = (10_000, 20, 10)

# Sweep-boundary collection telemetry.
_stats: dict[str, Any] = {
    'tuned': False,
    'original_thresholds': None,
    'active_thresholds': None,
    'sweep_collections': 0,
    'last_sweep_collect_ms': 0.0,
    'total_collected_objects': 0,
    'last_sweep_collect_utc': None,
}
_stats_lock = threading.Lock()


def tune_for_long_haul() -> None:
    """Install our tuned thresholds.  Idempotent; safe to call multiple
    times (the second+ call is a no-op).  Called from app startup."""
    global _TUNED
    with _TUNE_LOCK:
        if _TUNED:
            return
        try:
            original = gc.get_threshold()
            gc.set_threshold(*_TUNED_THRESHOLDS)
            with _stats_lock:
                _stats['tuned'] = True
                _stats['original_thresholds'] = list(original)
                _stats['active_thresholds'] = list(_TUNED_THRESHOLDS)
            log.info(
                'gc tuned for long-haul scan: thresholds %s → %s '
                '(defers gen-2 collections out of the scoring hot path)',
                original, _TUNED_THRESHOLDS,
            )
            _TUNED = True
        except Exception as exc:  # noqa: BLE001 — never let GC config break startup
            log.warning('gc tuning failed: %s', exc)


def collect_at_sweep_boundary(reason: str = 'sweep_wrap') -> None:
    """Force a full collection at a known idle moment.

    Called from `mark_batch_completed` when a universal-pass wraps.  The
    point is to spend the GC cost predictably (between passes) instead
    of randomly mid-batch.  Errors are swallowed because a failed
    collection should never crash the scan loop.

    Phase 26.33 also prunes expired entries from the per-symbol state
    dicts in `options_chain_service` and `daily_history_service` so
    they don't grow unboundedly across many universal passes.
    """
    from app.utils.time import utcnow_iso
    # ---- Step A: prune per-service expired state -------------------
    # Cheap (~ms) and frees memory before the GC walk.
    try:
        from app.services import options_chain_service
        options_chain_service.prune_expired_state()
    except Exception as exc:  # noqa: BLE001
        log.debug('gc sweep-prune: options_chain failed: %s', exc)
    try:
        from app.services import daily_history_service
        daily_history_service.prune_expired_state()
    except Exception as exc:  # noqa: BLE001
        log.debug('gc sweep-prune: daily_history failed: %s', exc)
    # Phase 26.60: reap any abandoned worker pools whose blocked threads
    # have exited naturally.  Prevents unbounded thread accumulation
    # across days of watchdog rebuilds even if no telemetry endpoint is
    # polled to trigger the on-demand reap.
    try:
        from app.services import options_chain_service as _ocs
        _ocs._reap_yf_abandoned_pools()
    except Exception as exc:  # noqa: BLE001
        log.debug('gc sweep-prune: options_chain abandoned-pool reap failed: %s', exc)
    try:
        from app.services import snapshot_store as _ss
        _ss._reap_abandoned_snap_pools()
    except Exception as exc:  # noqa: BLE001
        log.debug('gc sweep-prune: snapshot_store abandoned-pool reap failed: %s', exc)
    # ---- Step B: force the actual collection -----------------------
    try:
        t0 = time.monotonic()
        collected = gc.collect()
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        with _stats_lock:
            _stats['sweep_collections'] += 1
            _stats['last_sweep_collect_ms'] = round(elapsed_ms, 1)
            _stats['total_collected_objects'] += int(collected)
            _stats['last_sweep_collect_utc'] = utcnow_iso()
        log.info(
            'gc sweep-boundary collect (%s): freed %d objects in %.1f ms '
            '(total sweep collections: %d)',
            reason, collected, elapsed_ms, _stats['sweep_collections'],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning('gc sweep-boundary collect failed: %s', exc)


def gc_stats() -> dict:
    """Snapshot of GC telemetry for `/system/status.gc_stats`."""
    with _stats_lock:
        snap = dict(_stats)
    try:
        snap['current_thresholds'] = list(gc.get_threshold())
        snap['gen_counts'] = list(gc.get_count())
        snap['gen_stats'] = gc.get_stats()
    except Exception:  # noqa: BLE001
        pass
    return snap
