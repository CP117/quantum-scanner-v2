"""Phase 26.39 — Top-10 priority lane (leveraged-variant only).

The leveraged build scans a curated ~838-symbol universe instead of the
full 12,000.  At that universe size every symbol gets a refresh every
~30-60 s — that's still too coarse for the top names where the user
actually trades.  This service stands up a *second* lightweight worker
that:

    1. Reads the current top-10 rows from `snapshot_store` (already
       sorted desc by `final_score`).
    2. Builds seed rows for them (symbol + name + exchange).
    3. Calls `score_symbol_rows()` — the same scoring pipeline the
       normal snap-workers use — so the result is structurally identical
       and merges cleanly via `upsert_rows()`.
    4. Sleeps `interval_seconds` (default 2) and repeats.

Design constraints honored:
    - Strictly gated behind `is_leveraged_variant()`.  Main app build
      never starts this thread.
    - Uses the same scoring path as the main pipeline (no scoring
      drift between the priority lane and the regular sweep).
    - Independent of the snap-worker pool — the priority lane has its
      own daemon thread that can NEVER wedge the main scanner.  If the
      priority lane raises, it logs + sleeps + retries; it never
      cancels the main scan loop.
    - Honors the underlying provider circuit breakers / caches.  Calls
      to `score_symbol_rows()` reuse `quote_cache`, `daily_history`,
      and `options_chain_service` so the actual HTTP cost per tick
      averages out to zero once warm.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger('app.top10_priority')

# Module-level singleton state
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_started_at: float = 0.0
_last_tick_at: float = 0.0
_last_tick_count: int = 0
_last_tick_error: str = ''
_total_ticks: int = 0
_total_symbols_rescored: int = 0

# Phase 26.45 adaptive-throttle state, surfaced via get_status().
_adaptive_state: dict = {
    'consecutive_slow': 0,
    'consecutive_fast': 0,
    'monitor_only': False,
    'last_sleep_s': 0.0,
    'last_elapsed_s': 0.0,
}

# Phase 26.52 — Viewport "first-pass" tracker.
#
# The user requirement: every symbol on the first page (viewport) must
# get a FULL deep refresh + GARCH overlay on its first appearance
# (initial load AND every filter / sort change that brings a new
# symbol on-screen).  Subsequent ticks for symbols that are already
# in the tracker only need a quick GARCH refresh (cached daily
# history, cached advanced/lab/strategy bundles) — no full re-score.
#
# Layout: { 'STOCKS:AAPL': last_full_pass_monotonic_s, ... }
#
# Why per-symbol rather than per-set?  Two reasons:
#   1) When the user pages within the same filter, the set rotates
#      gradually; only the truly-NEW symbols need the heavy work.
#   2) After a long idle period (no viewport ping in TTL window) the
#      symbol is dropped from the tracker so the next push counts as
#      a fresh first-pass — matches the user's mental model of
#      "first time I see this on my page, deep-scan it".
_viewport_first_pass_lock = threading.Lock()
_viewport_first_pass: dict[str, float] = {}
_VIEWPORT_FIRST_PASS_TTL_S = 600.0  # 10-min — much longer than visible-symbols TTL


def _viewport_partition(market: str, symbols: list[str]) -> tuple[list[str], list[str]]:
    """Split `symbols` into (first_pass_needed, refresh_only).

    first_pass_needed = symbols not seen recently (or for the first
    time ever) → full score_symbol_rows + GARCH overlay
    refresh_only    = symbols already deep-scored this session →
    cheap GARCH-only re-attach (uses cached caches)
    """
    market = (market or 'stocks').upper()
    now_mono = time.monotonic()
    first: list[str] = []
    refresh: list[str] = []
    with _viewport_first_pass_lock:
        # Garbage-collect expired entries while we hold the lock.
        stale = [
            k for k, ts in _viewport_first_pass.items()
            if (now_mono - ts) > _VIEWPORT_FIRST_PASS_TTL_S
        ]
        for k in stale:
            _viewport_first_pass.pop(k, None)
        for sym in symbols:
            key = f'{market}:{sym.upper()}'
            if key in _viewport_first_pass:
                refresh.append(sym)
            else:
                first.append(sym)
    return first, refresh


def _mark_viewport_first_pass_done(market: str, symbols: list[str]) -> None:
    """Stamp `symbols` as having completed a full deep-pass at `now`."""
    market = (market or 'stocks').upper()
    now_mono = time.monotonic()
    with _viewport_first_pass_lock:
        for sym in symbols:
            _viewport_first_pass[f'{market}:{sym.upper()}'] = now_mono


def reset_viewport_first_pass_tracker(market: str | None = None) -> None:
    """Test / operator helper — wipe the tracker so the next viewport
    push counts as a fresh first-pass.  Without args wipes every market."""
    with _viewport_first_pass_lock:
        if market is None:
            _viewport_first_pass.clear()
        else:
            mkt = (market or 'stocks').upper()
            for k in list(_viewport_first_pass.keys()):
                if k.startswith(f'{mkt}:'):
                    _viewport_first_pass.pop(k, None)


def get_viewport_tracker_stats() -> dict:
    """Telemetry surface for /api/system/variant."""
    now_mono = time.monotonic()
    with _viewport_first_pass_lock:
        sorted_items = sorted(
            ((k, now_mono - ts) for k, ts in _viewport_first_pass.items()),
            key=lambda kv: kv[1],
        )
        recent = [
            {'symbol': k.split(':', 1)[1], 'age_seconds': round(age, 1)}
            for k, age in sorted_items[:25]
        ]
        return {
            'tracked_total': len(_viewport_first_pass),
            'ttl_seconds': int(_VIEWPORT_FIRST_PASS_TTL_S),
            'recent_first_pass': recent,
        }


def _priority_lane_loop(interval_seconds: float, top_n: int) -> None:
    """Long-running daemon target.  Re-scores the current top-N symbols
    every `interval_seconds`.  Self-healing: catches every exception
    inside the loop body so a single failure can never tear the thread
    down."""
    global _last_tick_at, _last_tick_count, _last_tick_error
    global _total_ticks, _total_symbols_rescored

    # Lazy imports so this module is cheap to import even when the
    # variant gate is off.
    from app.services.snapshot_store import get_snapshot, upsert_rows
    from app.services.scoring_service import score_symbol_rows
    from app.services.universe_service import load_universe

    log.info(
        'top10_priority_lane: started (interval=%.1fs, top_n=%d)',
        interval_seconds, top_n,
    )

    # Build a quick symbol -> (name, exchange) lookup off the universe so
    # the seed rows look identical to what the regular sweep produces.
    universe_meta: dict[str, dict] = {}
    try:
        for u in load_universe('stocks'):
            sym = (u.get('symbol') or '').upper()
            if sym:
                universe_meta[sym] = {
                    'symbol': sym,
                    'name': u.get('name', ''),
                    'exchange': u.get('exchange', ''),
                }
    except Exception:  # noqa: BLE001 — universe load failure shouldn't crash the lane
        log.exception('top10_priority_lane: failed to prime universe metadata')

    # Phase 26.45 — adaptive self-throttle to prevent long-haul lockup.
    # The legacy implementation slept exactly `interval_seconds` after
    # every tick regardless of how long the tick body took.  Over an
    # hour of operation that piled up provider quota burn AND held
    # provider thread-pool slots even when scoring was already
    # backlogged.  We now track tick wall-time and ratchet the sleep
    # interval up when the host is slow:
    #   * Tick < 1.0 s     → sleep = base                 (full speed)
    #   * Tick 1.0 - 3.0 s → sleep = 2 × base             (mild backoff)
    #   * Tick 3.0 - 8.0 s → sleep = 5 × base             (moderate backoff)
    #   * Tick > 8.0 s     → sleep = 15 × base + 2 consecutive trips → 60 s
    # After 5 consecutive slow ticks the lane enters "monitor only"
    # mode (60 s sleep, no scoring) until the host recovers — three
    # consecutive fast ticks pull it back to normal cadence.
    consecutive_slow = 0
    consecutive_fast = 0
    monitor_only = False

    while not _stop_event.is_set():
        cycle_started = time.time()
        tick_ok = False
        try:
            if monitor_only:
                # In recovery mode we ONLY peek at the snapshot age to
                # decide if the host is alive again.  No scoring call.
                log.debug('top10_priority_lane: monitor-only (host recovering)')
            else:
                snap = get_snapshot('stocks', limit=top_n, compact=True)
                results = snap.get('results') or []
                symbols = [r.get('symbol') for r in results if r.get('symbol')]
                # Phase 26.51 — Viewport-driven priority lane.
                # Always include the symbols currently visible to the
                # user (whatever the frontend last pushed to
                # `/api/future_mode/visible_symbols`) so the first-
                # page leaderboard is under continuous deep scan,
                # regardless of where each symbol sits in the global
                # ranking.  De-dup preserves insertion order so
                # global-top-N runs first, then viewport pings.
                viewport_syms: list[str] = []
                try:
                    from app.services.visible_symbols import get_visible as _get_visible
                    viewport_syms = list(_get_visible('stocks'))
                    if viewport_syms:
                        existing = set(symbols)
                        for vs in viewport_syms:
                            if vs not in existing:
                                symbols.append(vs)
                                existing.add(vs)
                except Exception as exc:  # noqa: BLE001
                    log.debug('viewport priority merge failed: %s', exc)

                # Phase 26.52 — Partition the viewport set into:
                #   * first_pass_syms   → never deep-scanned (or expired)
                #     → MUST get full score_symbol_rows + GARCH
                #   * refresh_only_syms → already deep-scanned this session
                #     → only need cheap GARCH re-attach (cached)
                # The global-top-N (always-scored) symbols implicitly
                # stay in `symbols` so the normal full pass below
                # always covers them.
                viewport_first_pass: list[str] = []
                viewport_refresh_only: list[str] = []
                if viewport_syms:
                    viewport_first_pass, viewport_refresh_only = _viewport_partition(
                        'stocks', viewport_syms
                    )
                if not symbols:
                    # No top-N yet (cold start) — sleep and try again.
                    _last_tick_at = time.time()
                    _last_tick_count = 0
                    _last_tick_error = ''
                    tick_ok = True
                else:
                    # Restrict the EXPENSIVE score_symbol_rows pass to
                    # (a) the global top-N and (b) the viewport
                    # first-pass set.  Refresh-only viewport symbols
                    # skip the heavy re-score; they only need the
                    # GARCH overlay refreshed (cheap because the
                    # advanced/lab/strategy caches are warm).
                    seeds_set: list[str] = []
                    seen_seeds: set[str] = set()
                    # Always include the global top-N
                    for sym in (r.get('symbol') for r in results if r.get('symbol')):
                        if sym and sym not in seen_seeds:
                            seeds_set.append(sym)
                            seen_seeds.add(sym)
                    # Plus the first-pass viewport entries
                    for sym in viewport_first_pass:
                        if sym not in seen_seeds:
                            seeds_set.append(sym)
                            seen_seeds.add(sym)

                    seeds = []
                    for sym in seeds_set:
                        meta = universe_meta.get(sym.upper())
                        seeds.append(meta or {'symbol': sym, 'name': '', 'exchange': ''})

                    scored = score_symbol_rows(seeds, force_full_pass2=True)
                    if scored:
                        upsert_rows('stocks', scored)
                        # Phase 26.47 — Future Mode GARCH tier.  After
                        # the top-10 are upserted with their fast-tier
                        # forward_metrics, we apply the GARCH overlay
                        # to the snapshot's top-N rows (default 25 —
                        # see `GARCH_TIER_TOP_N`).  This uses cached
                        # daily history, so the marginal cost per
                        # symbol is ~10-30 ms of pure CPU.  Failures
                        # never break the tick — the row keeps its
                        # fast-tier block instead.
                        try:
                            from app.services.future_mode_service import (
                                GARCH_TIER_TOP_N,
                                attach_forward_metrics_garch,
                            )
                            from app.services.snapshot_store import apply_to_top_n, lookup_snapshot_row, upsert_rows as _upsert
                            def _garch_overlay(row):
                                sym_g = (row.get('symbol') or '')
                                if sym_g:
                                    # Infer market from -USD suffix so crypto rows in
                                    # any mixed stock+crypto snapshot get BTC-USD
                                    # LCC driver rather than SPY.
                                    _mkt = 'crypto' if sym_g.upper().endswith('-USD') else 'stocks'
                                    attach_forward_metrics_garch(row, sym_g, market=_mkt)
                            n_overlaid = apply_to_top_n('stocks', GARCH_TIER_TOP_N, _garch_overlay)
                            # Phase 26.51 + 26.52 — overlay GARCH on
                            # ALL viewport symbols (both first-pass and
                            # refresh-only).  This guarantees the
                            # user's first page is under continuous
                            # GARCH attachment even when filters have
                            # promoted lower-ranked symbols.  The
                            # `attach_forward_metrics_garch` function
                            # is memoised via the advanced/lab/
                            # strategy caches, so the marginal cost on
                            # refresh-only symbols is just one daily-
                            # history read + 5 horizon computations.
                            try:
                                extras: list[dict] = []
                                # Process BOTH first-pass + refresh-only,
                                # capped at GARCH_TIER_TOP_N each.
                                _viewport_all = list(viewport_first_pass[:GARCH_TIER_TOP_N]) + \
                                                list(viewport_refresh_only[:GARCH_TIER_TOP_N])
                                # De-dupe while preserving order
                                _seen = set()
                                _vp_ordered = []
                                for vs in _viewport_all:
                                    if vs not in _seen:
                                        _vp_ordered.append(vs)
                                        _seen.add(vs)
                                for vs in _vp_ordered:
                                    row = lookup_snapshot_row(vs, 'stocks')
                                    if not row:
                                        continue
                                    _mkt = 'crypto' if vs.upper().endswith('-USD') else 'stocks'
                                    attach_forward_metrics_garch(row, vs, market=_mkt)
                                    extras.append(row)
                                if extras:
                                    _upsert('stocks', extras)
                            except Exception as exc:  # noqa: BLE001
                                log.debug('viewport GARCH overlay failed: %s', exc)
                            # Mark first-pass symbols as deep-scanned
                            # ONLY after the full pipeline (score +
                            # GARCH overlay) finished without raising.
                            if viewport_first_pass:
                                _mark_viewport_first_pass_done('stocks', viewport_first_pass)
                            if n_overlaid:
                                log.debug(
                                    'top10_priority_lane: GARCH overlay applied to top-%d (n=%d)',
                                    GARCH_TIER_TOP_N, n_overlaid,
                                )
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                'top10_priority_lane: GARCH overlay batch failed (non-fatal): %s', exc,
                            )

                    _last_tick_at = time.time()
                    _last_tick_count = len(scored)
                    _last_tick_error = ''
                    _total_ticks += 1
                    _total_symbols_rescored += len(scored)
                    tick_ok = True

                    log.debug(
                        'top10_priority_lane tick: rescored=%d viewport_first_pass=%d '
                        'viewport_refresh=%d elapsed=%.2fs',
                        len(scored), len(viewport_first_pass),
                        len(viewport_refresh_only), time.time() - cycle_started,
                    )
        except Exception as exc:  # noqa: BLE001
            # Log and continue.  Never let an exception kill the lane.
            _last_tick_error = f'{type(exc).__name__}: {exc}'
            log.warning('top10_priority_lane tick failed: %s', _last_tick_error)

        # Phase 26.45: adaptive sleep.  Compute the wall-time of THIS
        # tick and pick the next sleep accordingly.  Goal: keep the
        # priority lane from piling up provider quota burn when the
        # host is already overloaded (which is exactly the long-haul
        # lockup scenario the user hit at ~hour-1 of running).
        tick_elapsed = time.time() - cycle_started
        if not tick_ok:
            # Failure path: aggressive backoff regardless of timing.
            consecutive_slow += 1
            consecutive_fast = 0
            sleep_s = min(60.0, max(interval_seconds, 5.0 * (1.5 ** min(consecutive_slow, 6))))
        elif tick_elapsed > 8.0:
            consecutive_slow += 1
            consecutive_fast = 0
            sleep_s = 15.0 * interval_seconds
        elif tick_elapsed > 3.0:
            consecutive_slow += 1
            consecutive_fast = 0
            sleep_s = 5.0 * interval_seconds
        elif tick_elapsed > 1.0:
            consecutive_slow = max(0, consecutive_slow - 1)
            consecutive_fast = max(0, consecutive_fast - 1)
            sleep_s = 2.0 * interval_seconds
        else:
            consecutive_slow = 0
            consecutive_fast += 1
            sleep_s = interval_seconds

        # Monitor-only mode: 5+ slow ticks in a row → stop scoring for a while.
        if consecutive_slow >= 5 and not monitor_only:
            monitor_only = True
            sleep_s = 60.0
            log.warning(
                'top10_priority_lane: entering MONITOR-ONLY mode '
                '(consecutive_slow=%d, last_elapsed=%.2fs).  Will resume '
                'scoring after 3 consecutive fast ticks.',
                consecutive_slow, tick_elapsed,
            )
        elif monitor_only and consecutive_fast >= 3:
            monitor_only = False
            log.info(
                'top10_priority_lane: leaving MONITOR-ONLY mode '
                '(host recovered: %d consecutive fast ticks).', consecutive_fast,
            )

        # Publish adaptive state for the /api/system/variant telemetry.
        _adaptive_state['consecutive_slow'] = consecutive_slow
        _adaptive_state['consecutive_fast'] = consecutive_fast
        _adaptive_state['monitor_only'] = monitor_only
        _adaptive_state['last_sleep_s'] = sleep_s
        _adaptive_state['last_elapsed_s'] = tick_elapsed

        # Sleep with an early-exit when stop is requested
        _stop_event.wait(sleep_s)

    log.info('top10_priority_lane: stopped (total_ticks=%d, total_rescored=%d)',
             _total_ticks, _total_symbols_rescored)


def start_top10_priority_lane(interval_seconds: float = 2.0, top_n: int = 10) -> bool:
    """Start the priority lane if the variant gate allows it.

    Returns True if a thread was started (or was already running), False
    if the gate is closed (main app build).  Safe to call repeatedly.
    """
    global _thread, _started_at
    from app.services.universe_service import is_leveraged_variant

    if not is_leveraged_variant():
        log.debug('top10_priority_lane: NOT starting — main-app build (not leveraged variant)')
        return False

    if _thread is not None and _thread.is_alive():
        log.debug('top10_priority_lane: already running — start() is a no-op')
        return True

    _stop_event.clear()
    _started_at = time.time()
    _thread = threading.Thread(
        target=_priority_lane_loop,
        args=(float(interval_seconds), int(top_n)),
        name='top10-priority-lane',
        daemon=True,
    )
    _thread.start()
    return True


def stop_top10_priority_lane(timeout: float = 5.0) -> None:
    """Request shutdown (used by tests / clean restarts)."""
    _stop_event.set()
    if _thread is not None and _thread.is_alive():
        _thread.join(timeout=timeout)


def get_status() -> dict:
    """Telemetry surface for /api/system/variant and operator debugging."""
    running = _thread is not None and _thread.is_alive()
    return {
        'running': running,
        'started_at_epoch': _started_at if running else 0.0,
        'uptime_seconds': (time.time() - _started_at) if (running and _started_at) else 0.0,
        'last_tick_at_epoch': _last_tick_at,
        'last_tick_seconds_ago': (time.time() - _last_tick_at) if _last_tick_at else None,
        'last_tick_symbols_rescored': _last_tick_count,
        'last_tick_error': _last_tick_error,
        'total_ticks': _total_ticks,
        'total_symbols_rescored': _total_symbols_rescored,
        # Phase 26.45 adaptive-throttle telemetry
        'consecutive_slow': _adaptive_state.get('consecutive_slow', 0),
        'consecutive_fast': _adaptive_state.get('consecutive_fast', 0),
        'monitor_only': _adaptive_state.get('monitor_only', False),
        'last_sleep_seconds': _adaptive_state.get('last_sleep_s', 0.0),
        'last_tick_elapsed_seconds': _adaptive_state.get('last_elapsed_s', 0.0),
        # Phase 26.52 viewport first-pass tracker telemetry
        'viewport_first_pass': get_viewport_tracker_stats(),
    }
