"""
Server-side "live snapshot" store + backend-driven background scanner.

Why this exists
---------------
Before Phase 12 the frontend (app.js) drove the universe sweep itself: every
browser tab independently polled `/stocks/results?batch=N&limit=25` and
accumulated the rows in client-side memory.  That's fine for a single user on
the host machine, but it breaks the moment a second device (a phone on the
same WiFi, a laptop in the next room) connects:

  * the second browser starts its own batch sweep from batch 0,
  * every `/stocks/results` call re-runs the heavy `score_symbol_rows()`
    pipeline (multi-provider live fetch, intraday OHLC, options chain,
    factor families, ...),
  * so the *backend* now has 2x the load and *both* clients perceive a
    slower sweep,
  * and the secondary device never reaches batch ~493 in any reasonable
    time, leaving the user with only a handful of populated rows.

This module flips the architecture to a broadcast model:

  * a single asyncio background task ("scan loop") sweeps the universe on
    the backend and appends each batch's scored rows to an in-memory
    snapshot keyed by `(market, symbol)`,
  * every client - host PC, phone, second laptop - just polls
    `/api/scan/snapshot?market=...` and *mirrors* whatever the host has
    scored so far,
  * no per-client scoring, no per-client batch sweep.

The snapshot itself is intentionally simple: just the latest scored row per
symbol plus per-market scan progress.  No locking required on read because
Python's dict assignment is atomic; for writes we use a per-market lock so
concurrent batch writers (there's still only one, but defensive) can't
trample each other.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Iterable

log = logging.getLogger('app.snapshot')


# ---------------------------------------------------------------------------
# Phase 26.16 / Tier 1.1 — thinned-row LRU cache
# ---------------------------------------------------------------------------
# The compact `/api/scan/snapshot` payload requires clone-and-prune of every
# row in the bucket on every poll: per-family field filtering, factor-
# breakdown thinning, regulatory-signal extraction, etc.  At 4,750 rows ×
# 1–2 polls/s × multiple connected clients this is the loudest pure-CPU
# operation in the stack (see /app/docs/cpu_load_reduction_plan.md §1.1).
#
# Cache strategy:
#   * Key = (symbol, _snapshot_refreshed_at).  The monotonic timestamp is
#     stamped by `upsert_rows` every time a freshly-scored row is written,
#     so the key naturally invalidates when the row changes.
#   * LRU bounded to ~2× the largest bucket cap.  Evicting the LRU tail
#     bounds peak memory in pathological cases (rapid symbol turnover
#     during eviction).
#   * Concurrent reads are safe because the underlying OrderedDict is
#     mutated only inside `_thin_compact_row`, which is called from the
#     snapshot lock holder anyway (callers in `get_snapshot` hold
#     `_locks[market]` while building `results`).  A defensive
#     `_thin_cache_lock` guards us against future re-entry from non-locked
#     paths.
_THIN_CACHE_MAX = int(os.environ.get('SNAPSHOT_THIN_CACHE_MAX', '12000'))
_thin_cache: 'OrderedDict[tuple[str, float], dict]' = OrderedDict()
_thin_cache_lock = threading.Lock()
_thin_cache_hits = 0
_thin_cache_misses = 0


def _thin_cache_stats() -> dict:
    """Return cache observability for the providers-health page."""
    with _thin_cache_lock:
        total = _thin_cache_hits + _thin_cache_misses
        hit_rate = (_thin_cache_hits / total * 100.0) if total else 0.0
        return {
            'size': len(_thin_cache),
            'cap': _THIN_CACHE_MAX,
            'hits': _thin_cache_hits,
            'misses': _thin_cache_misses,
            'hit_rate_pct': round(hit_rate, 2),
        }


def _thin_cache_reset() -> None:
    """Drop all cached thinned rows. Used by `clear_snapshot()`."""
    global _thin_cache_hits, _thin_cache_misses
    with _thin_cache_lock:
        _thin_cache.clear()
        _thin_cache_hits = 0
        _thin_cache_misses = 0


# ============================================================================
# Phase 26.37 — Sorted-view cache & lock-free eviction
# ============================================================================
# Architecture problem this solves: previously, `upsert_rows` AND
# `get_snapshot` each performed a full O(N log N) sort of the bucket
# (~4,750 stock rows) under `_locks[market]`.  Post pass-1 (when the
# bucket fills to cap) every snap-worker batch + every UI poll hammered
# the same lock, and the lock-hold-while-sorting added up to 30-60% of
# wall time.  That's why the UI tabs spun forever after pass 1: every
# HTTP read of /api/scan/snapshot, /stock/{symbol}, or /system/status
# was queued behind a sort.
#
# Two cooperating optimizations:
#
# 1. `_sorted_view_cache[market]` — caches the sorted (symbol, row)
#    list keyed by `_bucket_version[market]`.  Every `upsert_rows`
#    increments the version (a single int, no extra work); the next
#    snapshot read sorts ONCE and caches; subsequent reads at the
#    same version are O(1).  Frontends polling at 1-5 s see the
#    common case of "no upsert between two polls" almost free.
#
# 2. `_evict_if_oversized` no longer sorts under the lock.  It copies
#    a list of (score, refreshed_at, has_reg_signal, sym) tuples out
#    of the bucket, releases the lock, runs `heapq.nsmallest(over,
#    ...)` (O(N + k log N) instead of O(N log N)) to pick the
#    eviction set, then re-acquires the lock just long enough to pop
#    those symbols.  Net lock-hold drops by ~10× per eviction.
#
# Backward-compatible: every caller's API is identical.  The cache is
# strictly an internal acceleration; behavior is observationally the
# same as a fresh sort each time.
_bucket_version: dict[str, int] = {'stocks': 0, 'crypto': 0}
# Each entry is (version, sorted_list_of_full_rows)
_sorted_view_cache: dict[str, tuple[int, list[dict]]] = {}
# Telemetry
_sorted_view_cache_stats = {
    'hits': 0, 'misses': 0,
}


def _invalidate_sorted_view(market: str) -> None:
    """Called under the market lock from every mutation path
    (upsert_rows, _evict_if_oversized, invalidate_symbol, clear_snapshot).
    Just bumps the version counter so the next sorted-view read knows the
    cache is stale.  Cheap (single int increment); the sort itself
    happens lazily on the next `get_snapshot` call (and OUTSIDE the
    lock — see `get_snapshot`)."""
    _bucket_version[market] = _bucket_version.get(market, 0) + 1


def sorted_view_cache_stats() -> dict:
    """Surfaced via /system/status for observability.

    Returned shape matches what the providers-health page expects:
        {'hits': int, 'misses': int, 'hit_rate_pct': float}
    """
    hits = _sorted_view_cache_stats.get('hits', 0)
    misses = _sorted_view_cache_stats.get('misses', 0)
    total = hits + misses
    hit_rate = (hits / total * 100.0) if total else 0.0
    return {
        'hits': hits,
        'misses': misses,
        'hit_rate_pct': round(hit_rate, 2),
    }



# Constants reused by `_thin_compact_row`. Hoisted to module scope so the
# tuple/set literals aren't rebuilt on every call.
_THIN_FAMILY_KEYS = (
    'trend_volume_delta', 'institutional_confluence',
    'options_positioning', 'institutional_order_block',
    'dark_pool_proxy', 'volume_sentiment',
    'reaction_clustering',
)
_THIN_FAMILY_FIELDS = frozenset({
    'score', 'bias', 'status', 'provenance', 'state',
    'classification', 'pressure_score_adjusted',
    'gamma_level_label', 'bucket', 'delta_pct',
})
_THIN_FB_KEEP = ('direction', 'tier', 'final_score', 'score_explanation', 'exit_penalty')
_THIN_MARKET_KEEP = ('source', 'change_pct', 'last_price',
                     'previous_close', 'provider_note', 'age_seconds')


def _build_thin_row(r: dict) -> dict:
    """Produce a thinned snapshot row (the heavy dict-pruning that the
    `compact=true` payload requires).  Pure function — no caching.

    Mirrors the previous inline logic in `get_snapshot()`; extracted so it
    can be memoized by `_thin_compact_row()`.
    """
    fb = r.get('factor_breakdown') or {}
    thin_fb: dict = {}
    sc = fb.get('secondary_composite')
    if sc:
        thin_fb['secondary_composite'] = sc
    # Renderer reads direction/tier/final_score off factor_breakdown for
    # legacy reasons, so keep them.
    for k in _THIN_FB_KEEP:
        if k in fb:
            thin_fb[k] = fb[k]
    mkt = fb.get('market') or {}
    thin_market: dict = {}
    for k in _THIN_MARKET_KEEP:
        if k in mkt:
            thin_market[k] = mkt[k]
    # Phase 26: preserve regulatory_signal so the row badge can render
    # without an extra /stock/{sym} fetch.
    reg_sig = mkt.get('regulatory_signal')
    if isinstance(reg_sig, dict):
        thin_market['regulatory_signal'] = reg_sig
    # Per-family payload pruning.
    for fam_key in _THIN_FAMILY_KEYS:
        fam = mkt.get(fam_key)
        if not isinstance(fam, dict):
            continue
        thin_fam = {k: v for k, v in fam.items() if k in _THIN_FAMILY_FIELDS}
        if thin_fam:
            thin_market[fam_key] = thin_fam
    if thin_market:
        thin_fb['market'] = thin_market
    em = fb.get('exit_model')
    if em is not None:
        thin_fb['exit_model'] = {
            'data_ready': (em.get('data_ready') if isinstance(em, dict) else None),
            'score': (em.get('score') if isinstance(em, dict) else None),
            'exit_flag': (em.get('exit_flag') if isinstance(em, dict) else None),
        }
    r_copy = dict(r)
    r_copy['factor_breakdown'] = thin_fb
    # Compact, top-level regulatory pill payload (Phase 26.13 + Phase 26.17).
    if isinstance(reg_sig, dict) and abs(reg_sig.get('applied_delta', 0) or 0) > 0.05:
        r_copy['regulatory'] = {
            'applied_delta': reg_sig.get('applied_delta', 0),
            'direction': reg_sig.get('direction', 'flat'),
            'event_count': reg_sig.get('event_count', 0),
            'cluster_bonus': reg_sig.get('cluster_bonus', 0.0),
            'bull_cluster_count': reg_sig.get('bull_cluster_count', 0),
            'bear_cluster_count': reg_sig.get('bear_cluster_count', 0),
            'top_role_weight': reg_sig.get('top_role_weight', 0.0),
            'staleness_days': reg_sig.get('staleness_days'),
            'aggregate_notional': reg_sig.get('aggregate_notional', 0.0),
            'signed_aggregate_notional': reg_sig.get('signed_aggregate_notional', 0.0),
            'cluster_event_count': reg_sig.get('cluster_event_count', 0),
        }
    return r_copy


def _thin_compact_row(r: dict) -> dict:
    """Memoized thin-row builder.  Cache key = (symbol, refreshed_at).

    On hit: returns the previously-thinned dict directly.  Because the
    snapshot payload is read-only (FastAPI serializes it to JSON and
    discards the reference), sharing the dict across polls is safe and is
    the entire point of the cache.

    On miss: builds the thinned row, stores it, and evicts the LRU tail
    if we're over capacity.
    """
    global _thin_cache_hits, _thin_cache_misses
    sym = r.get('symbol') or ''
    ver = r.get('_snapshot_refreshed_at') or 0.0
    key = (sym, ver)
    with _thin_cache_lock:
        cached = _thin_cache.get(key)
        if cached is not None:
            _thin_cache.move_to_end(key)
            _thin_cache_hits += 1
            return cached
        _thin_cache_misses += 1
    thinned = _build_thin_row(r)
    with _thin_cache_lock:
        _thin_cache[key] = thinned
        _thin_cache.move_to_end(key)
        # Evict the oldest entries when we're over the cap.  popitem(last=False)
        # pops the front (oldest) of the OrderedDict.
        while len(_thin_cache) > _THIN_CACHE_MAX:
            _thin_cache.popitem(last=False)
    return thinned


# ---------------------------------------------------------------------------
# Snapshot data structures
# ---------------------------------------------------------------------------
# {market: {symbol: row}}
_snapshot: dict[str, dict[str, dict]] = {'stocks': {}, 'crypto': {}}
# {market: {meta}}
_snapshot_meta: dict[str, dict[str, Any]] = {
    'stocks': {
        'last_full_sweep_at': None,    # ISO timestamp of most recent full universe sweep
        'last_batch_at': None,         # ISO timestamp of latest batch update
        'current_batch_index': 0,      # 0-based index of next batch the loop will process
        'highest_completed_batch': -1, # used by parallel-worker sweep-wrap detection
        'total_batches': 1,
        'universe_size': 0,
        'rows_scored': 0,              # cardinality of {symbol: row} (capped, top-N retained)
        # Phase 22: cumulative count of every symbol that has flowed through
        # the scoring system since startup.  Diverges from `rows_scored` once
        # the bucket cap kicks in, giving the user visibility that ALL
        # symbols in the universe really are being touched + ranked even
        # when only the top-N are retained in the visible table.
        'evaluations_ever': 0,
        # Phase 25: per-sweep counter — increments by the number of symbols
        # successfully scored in each batch, RESETS to 0 when the sweep
        # wraps.  This is the canonical "X of 12,301 scanned" number the
        # user expects to see counting up from 0 to total universe each
        # sweep.  Independent of bucket cap.
        'current_sweep_scanned': 0,
        'sweeps_completed': 0,
    },
    'crypto': {
        'last_full_sweep_at': None,
        'last_batch_at': None,
        'current_batch_index': 0,
        'highest_completed_batch': -1,
        'total_batches': 1,
        'universe_size': 0,
        'rows_scored': 0,
        'evaluations_ever': 0,
        'current_sweep_scanned': 0,
        'sweeps_completed': 0,
    },
}
_locks: dict[str, threading.Lock] = {
    'stocks': threading.Lock(),
    'crypto': threading.Lock(),
}


def upsert_rows(market: str, rows: Iterable[dict]) -> int:
    """Merge a freshly-scored batch into the snapshot.  Returns the post-merge
    row count for the market.  Called by the background scan loop (and could
    also be called by manual-refresh paths if we ever want them to update the
    broadcast snapshot directly).

    Phase 21: stamp each row with `_snapshot_refreshed_at` (monotonic) so the
    eviction pass can use it as the tiebreaker for score-weighted LRU.
    Cap the bucket size so multi-day runs don't grow process RSS unbounded.
    """
    market = market or 'stocks'
    accepted = 0
    now_mono = time.monotonic()
    with _locks.get(market, _locks['stocks']):
        bucket = _snapshot.setdefault(market, {})
        for row in rows:
            sym = (row or {}).get('symbol')
            if not sym:
                continue
            row['_snapshot_refreshed_at'] = now_mono
            # Phase 26.39: defense-in-depth — never demote a row from
            # full-depth scoring back to cheap.  If the new row tagged
            # itself `_score_depth='cheap'` and we already have a full
            # row in the bucket, lift the existing extended factor
            # families (institutional confluence, options positioning,
            # IOB, reaction clustering, volume sentiment, etc.) onto
            # the new row before storing.  The score and freshness
            # still update normally — only the extended-factor block
            # is preserved.  This stops the "composite breakdown bars
            # blank intermittently" bug at the source even if a future
            # caller forgets to pass `force_full_pass2=True`.
            existing = bucket.get(sym)
            # Phase 26.52 — UNIVERSAL extended-factor preservation.
            # Previously this block was gated on
            #   `row._score_depth == 'cheap' AND existing._score_depth == 'full'`
            # which left D-ranked symbols (never promoted to Pass 2 —
            # their `_score_depth` is always 'cheap') in the cold: their
            # extended factors were never preserved between ticks, so
            # institutional/options/dark-pool/etc dropped to zero on
            # every refresh.
            #
            # The merge is now invariant under depth labels: we always
            # preserve REAL non-zero extended-factor data when the
            # incoming row supplies a falsy/zero value at the same key.
            # When the new row genuinely improves on a factor (non-zero
            # real value), it wins — no change to that path.  This
            # never regresses the previous behaviour because:
            #   * cheap-on-full merges were already preserving real data
            #     under the previous gate.
            #   * full-on-full merges replace real with real (still
            #     wins because `_is_real(new_v)` is True).
            #   * cheap-on-cheap with real data on either side is now
            #     correctly merged — that's the new fix.
            if existing is not None:
                old_fb = existing.get('factor_breakdown') or {}
                new_fb = row.get('factor_breakdown') or {}
                old_mkt = old_fb.get('market') or {}
                new_mkt = new_fb.get('market') or {}
                # Keys that the extended-factor / Pass-2 pipeline populates.
                # Whenever any of these is real on the OLD row and falsy
                # on the NEW row, the OLD value wins.
                _ext_factor_keys = (
                    'trend_volume_delta',
                    'institutional_confluence',
                    'options_positioning',
                    'institutional_order_block',
                    'dark_pool_attraction',
                    'dark_pool_proxy',
                    'options_gamma',
                    'reaction_clustering',
                    'volume_sentiment',
                    'effort_vs_result',
                    'predictive_consensus',
                    'extended_factors',
                )

                def _is_real(v):
                    """A value is 'real' if it carries a non-zero score
                    or a non-empty payload.  Used to decide preservation
                    vs replacement on the merge."""
                    if v is None:
                        return False
                    if isinstance(v, dict):
                        s = v.get('score')
                        if s is None:
                            # dict without a `score` field but with
                            # other meaningful keys still counts.
                            return len(v) > 0
                        return (isinstance(s, str) and s.strip() != '') or (isinstance(s, (int, float)) and s != 0)
                    if isinstance(v, (int, float)):
                        return v != 0
                    if isinstance(v, (list, tuple, str)):
                        return len(v) > 0
                    return True

                merge_touched = False
                for key in _ext_factor_keys:
                    if key not in old_mkt:
                        continue
                    old_v = old_mkt.get(key)
                    new_v = new_mkt.get(key)
                    if _is_real(old_v) and not _is_real(new_v):
                        new_mkt[key] = old_v
                        merge_touched = True
                # Narratives + secondary composite: cheap pass never
                # produces these, full pass does — carry forward.
                if old_fb.get('factor_narratives') and not new_fb.get('factor_narratives'):
                    new_fb['factor_narratives'] = old_fb['factor_narratives']
                    merge_touched = True
                if old_fb.get('secondary_composite') and not new_fb.get('secondary_composite'):
                    new_fb['secondary_composite'] = old_fb['secondary_composite']
                    merge_touched = True
                if merge_touched and old_mkt:
                    new_fb['market'] = new_mkt
                    row['factor_breakdown'] = new_fb
            # Phase 26.50 bugfix — preserve the priority-lane GARCH overlay
            # across scanner ticks.  The scanner re-scores rows on every
            # sweep but only the priority-lane attaches `forward_metrics_garch`.
            # Without this carry-over, the GARCH block would be silently
            # destroyed on the very next scanner tick, leaving every row
            # showing "fast" tier in the leaderboard until the priority
            # lane fired on it again (~12 s later if it ever did).
            #
            # The same problem affected filter changes (Bulls/Bears,
            # intensity bands): newly-promoted rows would render fast-tier
            # only because the GARCH block had been wiped on a previous
            # tick before they made the cut.
            #
            # We carry forward any non-empty `forward_metrics_garch` from
            # the OLD row when the NEW row didn't supply one.  Same for the
            # higher-tier overlays the priority lane stamps on its rows.
            if existing is not None:
                _PRIORITY_LANE_PRESERVE = (
                    'forward_metrics_garch',
                    'priority_lane_attached_at',
                    'priority_lane_tier',
                )
                for fld in _PRIORITY_LANE_PRESERVE:
                    if existing.get(fld) and not row.get(fld):
                        row[fld] = existing[fld]
                # advanced/lab/strategy signals are RECOMPUTED on every
                # cheap pass, but the cheap pass may legitimately set them
                # to None if there's not enough history *this tick*.  If
                # the new row sets one to None and the old row had real
                # data, preserve the old.  (Real data is more useful than
                # a transient None caused by a throttled provider.)
                _OVERLAY_PRESERVE = ('advanced_signals', 'lab_signals', 'strategy_signals')
                for fld in _OVERLAY_PRESERVE:
                    if existing.get(fld) and row.get(fld) is None:
                        row[fld] = existing[fld]
            bucket[sym] = row
            accepted += 1
        evicted = _evict_if_oversized(market, bucket)
        if evicted:
            log.debug('snapshot bucket evicted %d rows (market=%s, post-evict=%d)',
                      evicted, market, len(bucket))
        # Phase 26.37: any successful upsert (or eviction) reorders the
        # bucket relative to final_score, so bump the sorted-view cache
        # version.  A single int increment under the same lock — no
        # extra contention.  The next get_snapshot() will then know it
        # needs to recompute the sorted view (and will do so OUTSIDE
        # the lock).
        if accepted or evicted:
            _invalidate_sorted_view(market)
        meta = _snapshot_meta.setdefault(market, {})
        meta['rows_scored'] = len(bucket)
        # Phase 22: cumulative counter independent of bucket cap so the user
        # can confirm the scanner really is sweeping every symbol in the
        # universe even after eviction kicks in.
        meta['evaluations_ever'] = int(meta.get('evaluations_ever', 0)) + accepted
        # Phase 25: per-sweep counter that the dashboard's "X / 12,301
        # scanned" headline reads from.  Reset to 0 inside
        # mark_batch_completed() when the sweep wraps.
        meta['current_sweep_scanned'] = int(meta.get('current_sweep_scanned', 0)) + accepted
    return accepted


def mark_batch_completed(market: str, batch_index: int, total_batches: int, universe_size: int) -> None:
    """Update the per-market progress meta after a batch finishes.

    Phase 24: with parallel workers `_sweep_one_batch` pre-claims its
    batch by bumping `current_batch_index` ATOMICALLY before calling
    the slow scoring path.  This function therefore must NOT regress
    that counter when a worker that claimed an earlier slot finishes
    after a worker that claimed a later slot.

    Semantics:
      - `current_batch_index` = next slot to be claimed (monotonically
        increases, mod total_batches).  Set by `_sweep_one_batch`.
      - `highest_completed_batch` = most recent finished batch index.
        Set here.  Used purely for telemetry.
      - Sweep wrap (`sweeps_completed += 1`) fires when the just-
        completed batch index equals `total_batches - 1`.

    The wrap also resets `current_batch_index` back to 0 if the
    pre-claim has run past total_batches (which it will, since the
    pre-claim doesn't know how many batches are valid).
    """
    from app.utils.time import utcnow_iso
    with _locks.get(market, _locks['stocks']):
        meta = _snapshot_meta.setdefault(market, {})
        meta['last_batch_at'] = utcnow_iso()
        meta['total_batches'] = max(1, int(total_batches or 1))
        meta['universe_size'] = int(universe_size or 0)
        completed = int(batch_index)
        meta['highest_completed_batch'] = max(int(meta.get('highest_completed_batch', -1)), completed)
        # Did we just finish the last batch in the sweep?  Detect via
        # the highest-completed counter rather than the volatile
        # current_batch_index (which a parallel worker may have already
        # advanced past the end).
        if meta['highest_completed_batch'] >= meta['total_batches'] - 1:
            meta['last_full_sweep_at'] = meta['last_batch_at']
            meta['sweeps_completed'] = int(meta.get('sweeps_completed', 0)) + 1
            meta['highest_completed_batch'] = -1
            meta['current_batch_index'] = 0
            # Phase 25: reset the per-sweep counter so the dashboard
            # headline visibly cycles 0 → 12,301 → 0 → 12,301 ... on
            # every full sweep, never plateauing below the universe size.
            meta['current_sweep_scanned'] = 0
            # Phase 26.33: force a full GC at the sweep boundary.  This
            # moves the expensive gen-2 walk OUT of the scoring hot
            # path and into a known-idle moment, eliminating the
            # random 10%↔100% CPU spikes the user observed during
            # pass 2+.  Cheap (~200-500 ms) and only fires once per
            # ~12,000-symbol pass.
            try:
                from app.services.gc_service import collect_at_sweep_boundary
                collect_at_sweep_boundary(reason=f'{market}_sweep_wrap')
            except Exception:  # noqa: BLE001
                pass
        else:
            # Keep current_batch_index in legal range — if a worker
            # over-claimed (e.g. the universe shrunk mid-sweep), clamp
            # it down so we don't process phantom batches.
            cbi = int(meta.get('current_batch_index', 0))
            if cbi >= meta['total_batches']:
                meta['current_batch_index'] = cbi % meta['total_batches']


def get_snapshot(market: str, limit: int = 1500, compact: bool = True,
                 sort: str = 'score') -> dict:
    """Return the broadcast snapshot for the given market.

    Phase 19 speed fix (CRITICAL):
    -----------------------------
    The V1 endpoint returned ALL rows on every poll. At 3,000+ scored
    rows × ~15 KB each = 44 MB of JSON per request. With multiple
    clients polling every 3-4 seconds AND the scan loop trying to
    write into the same store, the snapshot endpoint was choking the
    scan loop (lock contention + CPU-bound JSON serialization).

    Two-knob fix:
      - `limit`: cap rows returned to the top-N by composite score
        (default 1500 -- well past anything the UI pages through, but
        ~3x smaller than the full universe at steady-state).
      - `compact`: strip the heavyweight `factor_narratives` payload
        from every row when true (default). Narratives are only used
        by the detail panel which fetches its own per-symbol data via
        `/api/detail/{symbol}`. Drops per-row size from ~15 KB to ~6 KB.

    Combined: 44 MB -> ~9 MB per response (-80%). Lock-hold time drops
    proportionally because the sort+copy under the lock is now faster.

    Shape mirrors `/stocks/results`:
      {
        'market', 'universe_size', 'total_batches', 'rows_scored',
        'sweeps_completed', 'last_batch_at', 'last_full_sweep_at',
        'current_batch_index', 'results': [row, row, ...],
        'results_truncated_to_top_n': int,   # new in Phase 19
      }
    """
    market = market or 'stocks'
    limit = max(1, min(int(limit or 1500), 10000))

    # Phase 26.37: lock-light read path.
    #
    # Old design: held `_locks[market]` while running
    # `sorted(bucket.values(), ...)` over ~4,750 rows on EVERY poll.
    # Multiple HTTP clients × 1-5s polling + 2 snap-workers writing
    # produced a lock convoy that visibly froze the UI at the start of
    # pass 2.
    #
    # New design:
    #   1. Under lock (brief): check the version cache.  If hit, grab
    #      the cached sorted list directly.  If miss, copy out the row
    #      references (a shallow copy of dict pointers — fast) and the
    #      current version number.  Also grab the meta dict.
    #   2. OUTSIDE the lock: run `sorted()` on the local copy.  This is
    #      where the multi-millisecond work happens — and now no other
    #      thread is blocked behind it.
    #   3. Under lock (brief): if the bucket version is still what we
    #      sorted against, publish the result into the cache.  If the
    #      bucket was mutated mid-sort, we skip the publish (a future
    #      reader will resort) but still return the freshly-sorted
    #      result to the current caller.
    cached_view: list[dict] | None = None
    sort_version: int = 0
    row_refs: list[dict] | None = None
    meta: dict
    with _locks.get(market, _locks['stocks']):
        bucket = _snapshot.get(market, {})
        sort_version = _bucket_version.get(market, 0)
        cached = _sorted_view_cache.get(market)
        if cached is not None and cached[0] == sort_version:
            cached_view = cached[1]
            _sorted_view_cache_stats['hits'] = _sorted_view_cache_stats.get('hits', 0) + 1
        else:
            # Shallow-copy references out of the bucket so we can sort
            # without holding the lock.  Each element is still the
            # same dict object the writer mutates; that's fine because
            # we only read `final_score` (an immutable scalar) during
            # the sort.
            row_refs = list(bucket.values())
            _sorted_view_cache_stats['misses'] = _sorted_view_cache_stats.get('misses', 0) + 1
        meta = dict(_snapshot_meta.get(market, {}))

    if cached_view is not None:
        all_results = cached_view
    else:
        # Heavy sort happens HERE, with no market lock held.
        assert row_refs is not None  # for the type checker
        all_results = sorted(row_refs, key=lambda r: -(r.get('final_score') or 0))
        # Try to publish — but only if the bucket hasn't been mutated
        # since we copied the refs.  Otherwise the cached order would
        # be stale (still useful — but a fresher reader will overwrite
        # it momentarily anyway, so just skip the publish).
        with _locks.get(market, _locks['stocks']):
            if _bucket_version.get(market, 0) == sort_version:
                _sorted_view_cache[market] = (sort_version, all_results)

    total_rows = len(all_results)
    # Predicted-volume-first ordering: an explicit ranking stage that
    # surfaces likely upcoming high-volume names BEFORE truncation, so
    # downstream filters refine an already-prioritized set.
    if sort == 'predicted_volume_intensity':
        all_results = sorted(
            all_results,
            key=lambda r: (-(r.get('predicted_volume_intensity_score') or 0.0),
                           -(r.get('final_score') or 0.0)),
        )
    results = all_results[:limit]

    if compact:
        # Strip heavy fields the leaderboard doesn't need. The detail
        # panel fetches its own data via /api/detail/{symbol} which has
        # the full breakdown including narratives, reaction_map zones,
        # ratings sub-objects, exit/stability model traces, etc.
        # Per-row size drops from ~12 KB to ~1.5 KB (-87%).
        #
        # Phase 26.16 / Tier 1.1: cache the thinned dict per (symbol,
        # _snapshot_refreshed_at). Multiple snapshot polls in a row for
        # rows that didn't change since the last write skip the dict-
        # cloning + factor-breakdown-pruning entirely. The cache uses
        # the row's monotonic refresh timestamp as its version key, so
        # the next time `record_batch_rows` writes the same symbol with
        # a newer timestamp the stale thin row is invalidated naturally.
        compact_results = [_thin_compact_row(r) for r in results]
        results = compact_results

    return {
        'market': market,
        'universe_size': meta.get('universe_size', 0),
        'total_batches': meta.get('total_batches', 1),
        'rows_scored': total_rows,
        'evaluations_ever': meta.get('evaluations_ever', 0),
        # Phase 25: per-sweep monotonic counter (0 → universe_size → 0 ...)
        'current_sweep_scanned': meta.get('current_sweep_scanned', 0),
        'sweeps_completed': meta.get('sweeps_completed', 0),
        'last_batch_at': meta.get('last_batch_at'),
        'last_full_sweep_at': meta.get('last_full_sweep_at'),
        'current_batch_index': meta.get('current_batch_index', 0),
        'results': results,
        'results_truncated_to_top_n': limit if total_rows > limit else None,
        'compact_mode': bool(compact),
        'sort_mode': sort,
    }


def lookup_snapshot_row(symbol: str, market: str = 'stocks') -> dict | None:
    """Return the FULL (non-compact) row for `symbol` from the in-memory
    snapshot, or None if the symbol hasn't been scored yet.

    Phase 26.36 (detail-panel consistency fix): the detail endpoint used
    to fire its own parallel fetch and re-score from scratch, producing
    scores that drifted from what the leaderboard showed for the same
    symbol.  Worse, if the detail's live-quote fetch failed (Yahoo
    rate-limited, Stooq circuit-open), the detail re-score fell back to
    cached quotes and stamped the row with a "cache_fallback" provider
    outcome — which is what was making the detail panel show CACHE for
    a symbol the list correctly showed as YFINANCE/fresh.

    The cleanest fix is to make the detail endpoint trust the snapshot
    store as the source of truth for the score AND the freshness
    label.  This function is the O(1) accessor that supports that.
    Returns a *copy* so callers can't mutate the canonical state.
    """
    if not symbol:
        return None
    market = market or 'stocks'
    sym = symbol.upper()
    with _locks.get(market, _locks['stocks']):
        bucket = _snapshot.get(market, {})
        row = bucket.get(sym)
        # Try alternate market if not found (e.g. crypto symbols with -USD
        # suffix may live in the crypto bucket regardless of caller hint).
        if row is None and market != 'crypto':
            alt = _snapshot.get('crypto', {})
            row = alt.get(sym)
        if row is None:
            return None
        # Defensive copy: callers (detail_service) may add fundamentals
        # and other fields; we don't want those leaking back into the
        # leaderboard data.
        return dict(row)


def get_snapshot_meta(market: str | None = None) -> dict:
    """Lightweight progress probe (no result rows).  Used by /system/status."""
    if market:
        return dict(_snapshot_meta.get(market, {}))
    return {m: dict(_snapshot_meta.get(m, {})) for m in ('stocks', 'crypto')}


def apply_to_top_n(market: str, top_n: int, callback) -> int:
    """Phase 26.47 — atomically mutate the top-N rows under the market
    lock by running `callback(row)` on each of the highest-final_score
    rows.  Used by Future Mode's GARCH tier (top10_priority_service):
    the lane scores top-10 directly, then applies GARCH attachment to
    snapshot positions 1..N (default 25) in-place.

    Why this helper instead of upsert_rows() ?

      * `callback` typically attaches `forward_metrics_garch` to a row
        that already lives in the bucket — we don't have a freshly-
        scored row to upsert, just a small overlay.
      * Going through upsert_rows() with a partial row would re-trigger
        the depth-preservation merge logic and is more bookkeeping
        than this overlay needs.
      * Mutating in-place under the lock is atomic for snapshot
        readers — they either see the old row or the new one, never a
        half-applied state.

    Returns the number of rows the callback was applied to.  Failures
    in callback are caught + logged so a single bad row can't poison
    the whole top-N pass.
    """
    market = market or 'stocks'
    top_n = max(0, int(top_n or 0))
    if top_n == 0:
        return 0
    applied = 0
    with _locks.get(market, _locks['stocks']):
        bucket = _snapshot.get(market, {})
        if not bucket:
            return 0
        # Sort references by final_score desc and take the top N.
        ordered = sorted(
            bucket.values(),
            key=lambda r: float(r.get('final_score') or 0),
            reverse=True,
        )[:top_n]
        for row in ordered:
            try:
                callback(row)
                # Bump the snapshot-refreshed timestamp so the thin-cache
                # invalidates and downstream pollers re-thin the row.
                row['_snapshot_refreshed_at'] = time.monotonic()
                applied += 1
            except Exception as exc:  # noqa: BLE001
                log.debug('apply_to_top_n callback failed for %s: %s',
                          row.get('symbol'), exc)
        # Mutating row contents shouldn't change ordering (callback
        # only adds overlay fields), but the thin-cache versions need
        # to invalidate so polls see the new fields.
        _invalidate_sorted_view(market)
    return applied


# ---------------------------------------------------------------------------
# Background scan loop
# ---------------------------------------------------------------------------
# Tunables.  Defaults give a roughly 12-15 minute full sweep of a ~12.3k
# stock universe + a ~60s full sweep of the ~2.5k crypto universe on the
# host PC, leaving plenty of headroom for HTTP requests.  Override via
# env vars on slower machines.
#
# Note: smaller batch sizes + longer inter-batch sleeps yield SMOOTHER UI
# updates (the snapshot grows incrementally) and lower per-batch latency
# (HTTP requests aren't blocked for as long).  Counter-intuitively this is
# more responsive than scoring 50 symbols at once in a single tight loop.
_BATCH_LIMIT_STOCKS = int(os.environ.get('SNAPSHOT_BATCH_LIMIT_STOCKS', '100'))
_BATCH_LIMIT_CRYPTO = int(os.environ.get('SNAPSHOT_BATCH_LIMIT_CRYPTO', '50'))
_INTER_BATCH_SLEEP = float(os.environ.get('SNAPSHOT_INTER_BATCH_SLEEP', '0.5'))  # seconds
_INTER_MARKET_SLEEP = float(os.environ.get('SNAPSHOT_INTER_MARKET_SLEEP', '0.75'))
_LOOP_FAILURE_BACKOFF = float(os.environ.get('SNAPSHOT_FAILURE_BACKOFF', '5.0'))

# Phase 24: parallel-batch worker count.  Set 1 to restore the old
# sequential behaviour.  2-3 is the sweet spot — more than that
# saturates Yahoo's per-IP rate limit and the gains evaporate.
_PARALLEL_WORKERS = max(1, int(os.environ.get('SNAPSHOT_PARALLEL_WORKERS', '2')))

# ---------------------------------------------------------------------------
# Phase 21: bounded ring-buffer per market.
# ---------------------------------------------------------------------------
# Without this cap the bucket would grow to the full universe (12k+ rows).
# Each row holds a ~10 KB factor breakdown, so a saturated bucket carries
# ~120 MB in process memory.  Every poll triggers a sorted() + compact pass
# over the bucket which becomes the dominant CPU cost over multi-day runs.
#
# The cap keeps the top-N "interesting" rows in memory.  When a new row
# would push us over the cap, we evict the row with the LOWEST composite
# score that hasn't been refreshed in the longest time.  This is a
# score-weighted LRU: high-score rows stick around even if rarely
# re-scored, while low-score rows that haven't seen a refresh in a while
# get aged out first.
#
# Defaults (env-overridable):
#   stocks: 5,000 rows (~50 MB)   — well above what any UI ever shows
#   crypto: 2,500 rows (~25 MB)
_MAX_ROWS_STOCKS = int(os.environ.get('SNAPSHOT_MAX_ROWS_STOCKS', '4750'))
_MAX_ROWS_CRYPTO = int(os.environ.get('SNAPSHOT_MAX_ROWS_CRYPTO', '2500'))


def _bucket_limit(market: str) -> int:
    return _MAX_ROWS_CRYPTO if market == 'crypto' else _MAX_ROWS_STOCKS


def _evict_if_oversized(market: str, bucket: dict[str, dict]) -> int:
    """Evict the lowest-priority rows when the bucket exceeds its cap.

    Phase 26.15.b: rows that carry an active regulatory signal (insider
    activity, contract awards) are PROTECTED from eviction unless the
    bucket is so oversized that we have to evict them too. This fixes the
    "regulatory-boosted symbol pops into the dashboard then vanishes
    seconds later" bug - the boost itself was working, but the row was
    being kicked out of the 4750-row bucket the moment a non-boosted row
    with a slightly higher score landed.

    Priority key (lowest = evicted first):
      1) `has_regulatory_signal` -> non-boosted rows go first
      2) `final_score`           -> lowest score next
      3) `refreshed_at`          -> oldest refresh as the tiebreaker

    Phase 26.37 (P1): replaced the previous `sorted(bucket.items())`
    (O(N log N)) with `heapq.nsmallest(over, ...)` (O(N log k), where
    `k = over` is normally ≤ the batch size, ~100).  On a 4,750-row
    bucket with a 100-row overflow this is roughly 10× faster.

    The caller holds `_locks[market]` while invoking this.  The heap
    selection is fast enough that doing it under the lock is fine —
    moving it OUTSIDE the lock would require a two-phase approach
    (copy refs, drop lock, heap, re-acquire, pop) and add a race
    window for new upserts hitting the same symbols.  Keep it
    contained for correctness; the heap itself is the optimization.
    """
    import heapq

    cap = _bucket_limit(market)
    over = len(bucket) - cap
    if over <= 0:
        return 0

    def _has_reg_signal(row: dict) -> bool:
        # Phase 26.18.f: the *stored* bucket rows carry the regulatory
        # signal under `factor_breakdown.market.regulatory_signal` — the
        # top-level `regulatory` key is only added by `_build_thin_row()`
        # on the way OUT to compact-mode HTTP responses, so checking it
        # here was always reading `{}` and effectively disabled the
        # protection. We now look at the stored location (with a fallback
        # to the top-level key for any future code that adds it).
        reg = row.get('regulatory')
        if not isinstance(reg, dict):
            fb = row.get('factor_breakdown') or {}
            mkt = fb.get('market') or {}
            reg = mkt.get('regulatory_signal') or {}
        # Treat the row as "regulatory-flagged" only when the applied
        # delta is materially non-zero. Otherwise we'd protect every row
        # that ever had a stale signal record attached.
        return abs(float(reg.get('applied_delta') or 0.0)) >= 0.5

    # heapq.nsmallest returns the `over` lowest-priority entries in
    # ascending order.  Same semantics as the previous
    # `sorted(...)[:over]` slice, just faster.
    candidates = heapq.nsmallest(
        over,
        bucket.items(),
        key=lambda kv: (
            1 if _has_reg_signal(kv[1]) else 0,
            float(kv[1].get('final_score') or 0),
            float(kv[1].get('_snapshot_refreshed_at') or 0),
        ),
    )
    for sym, _row in candidates:
        bucket.pop(sym, None)
    # The bucket layout changed — any cached sorted view is stale.
    # (`upsert_rows` also invalidates after merge+evict, so this is
    # belt-and-suspenders for any future direct caller.)
    _invalidate_sorted_view(market)
    return over


_loop_started = False
_loop_started_lock = threading.Lock()


# Phase 26.60: abandoned-pool reaper for the snapshot watchdog.
# When the watchdog fires (no batch completion for _WATCHDOG_STALL_S),
# it calls `pool.shutdown(wait=False)` on the current snap-worker pool
# and spins up a fresh one.  Historically the old pool was dropped
# without any tracking — its worker threads (blocked on hung Yahoo /
# provider HTTP reads) stayed pinned until the OS reaped the socket,
# leaking thread state indefinitely.  Track them so a subsequent read
# of `abandoned_pools_stats()` can drop references to fully-drained
# pools and prevent unbounded accumulation across many rebuilds.
_ABANDONED_SNAP_POOLS_LOCK = threading.Lock()
_ABANDONED_SNAP_POOLS: list = []  # list[tuple[float, ThreadPoolExecutor]]


def _reap_abandoned_snap_pools() -> dict:
    """Drop any abandoned snap-worker pool whose worker threads have
    all exited naturally.  Returns telemetry about what's still leaking.
    Uses `pool._threads` (stable across CPython 3.x); degrades to
    "assume leaking" if introspection fails.
    """
    drained = 0
    remaining_pools = 0
    remaining_threads = 0
    with _ABANDONED_SNAP_POOLS_LOCK:
        keep: list = []
        for abandoned_at, pool in _ABANDONED_SNAP_POOLS:
            try:
                alive = [t for t in getattr(pool, '_threads', ()) if t.is_alive()]
            except Exception:  # noqa: BLE001
                alive = ['unknown']
            if not alive:
                drained += 1
                continue
            keep.append((abandoned_at, pool))
            remaining_pools += 1
            remaining_threads += len(alive)
        _ABANDONED_SNAP_POOLS[:] = keep
    return {
        'drained_this_call': drained,
        'remaining_pools': remaining_pools,
        'remaining_threads': remaining_threads,
    }


def abandoned_snap_pools_stats() -> dict:
    """Telemetry — surfaced via /system/status so operators can see
    whether the watchdog is systematically leaking threads (a warning
    sign that the pool concurrency ceiling needs adjustment or a
    provider is chronically hung)."""
    return _reap_abandoned_snap_pools()


def _sweep_one_batch(market: str) -> None:
    """Compute one batch for `market`, write the result rows into the
    snapshot, and advance the per-market batch pointer.

    Phase 24: atomic batch-index allocation under the per-market lock so
    parallel workers don't double-process the same batch.

    Phase 25: when `market='crypto'` and the user hasn't engaged the
    crypto view in the activity TTL window, we walk the batch index
    forward WITHOUT firing any provider HTTP calls.  Existing rows in
    the snapshot bucket stay put (they decay naturally via the
    score-weighted LRU eviction), the stock scanner gets the entire
    HTTP budget, and the crypto pipeline re-activates the moment the
    user touches `/api/scan/snapshot?market=crypto` or a crypto detail
    endpoint.
    """
    from app.services.result_store import get_results_batch
    from app.services.market_activity_service import is_active
    from app.services.universe_service import is_crypto_active

    # Phase 26.66: crypto scans only when ≥1 crypto universe group is
    # active.  No active crypto group → skip the market entirely (no batch
    # claim, no provider HTTP).  Replaces the old leveraged-variant
    # `is_crypto_disabled()` short-circuit; crypto is now opt-in via the
    # universe toggles regardless of build variant.
    if market == 'crypto' and not is_crypto_active():
        return

    # Atomic claim: increment the per-market batch counter inside the lock
    # so two parallel workers always get different batch indices.
    with _locks.get(market, _locks['stocks']):
        meta = _snapshot_meta.setdefault(market, {})
        batch_idx = int(meta.get('current_batch_index', 0))
        meta['current_batch_index'] = batch_idx + 1
        # First-sweep bootstrap check: has crypto EVER produced rows since
        # this process started?  If not, we ignore the activity gate below
        # and force a real batch so the snapshot bucket gets populated.
        crypto_first_sweep = (
            market == 'crypto'
            and len(_snapshot.get('crypto') or {}) == 0
            and int(meta.get('sweeps_completed', 0)) == 0
        )

    if market == 'crypto' and not is_active('crypto') and not crypto_first_sweep:
        # Idle pass: advance the batch pointer (so universe sweep still
        # completes for accounting purposes) without firing CoinGecko /
        # CryptoCompare / CoinPaprika requests.  Saves ~30-40% of total
        # HTTP traffic when the user is stocks-only.
        #
        # BUT: on the FIRST sweep after process start we skip this idle
        # path (see `crypto_first_sweep` above) so the crypto bucket + the
        # `universe_size` meta field are populated with real data even
        # if the user hasn't yet clicked the Crypto tab.  Otherwise the UI
        # showed "0 universe / 0 rows" until the user's first crypto poll
        # AND enough time for the scan loop to cycle back to crypto — a
        # 5-10 minute window that felt like a hard failure.
        with _locks.get(market, _locks['stocks']):
            meta = _snapshot_meta.setdefault(market, {})
            total_batches = max(1, int(meta.get('total_batches', 1)))
            # Populate universe_size even on idle passes so the UI can
            # display the correct total from the moment the user opens
            # the crypto tab, without waiting for a real batch.
            if not meta.get('universe_size'):
                try:
                    from app.services.universe_service import get_universe
                    meta['universe_size'] = len(get_universe('crypto'))
                except Exception:  # noqa: BLE001
                    pass
        mark_batch_completed(market, batch_idx, total_batches, meta.get('universe_size', 0))
        return

    limit = _BATCH_LIMIT_CRYPTO if market == 'crypto' else _BATCH_LIMIT_STOCKS
    envelope = get_results_batch(batch=batch_idx, limit=limit, market=market)
    rows = envelope.get('results') or []
    if rows:
        upsert_rows(market, rows)
    mark_batch_completed(
        market,
        envelope.get('current_batch', batch_idx),
        envelope.get('total_batches', meta.get('total_batches', 1)),
        envelope.get('total', meta.get('universe_size', 0)),
    )


def _scan_loop_target() -> None:
    """Background thread entrypoint.  Phase 24 redesign: alternates between
    stocks and crypto markets, processing `_PARALLEL_WORKERS` batches
    concurrently per market sweep before moving on.

    With parallel=2 (default) we issue two stock batches simultaneously
    against different symbol slices, doubling provider throughput at the
    cost of slightly higher peak HTTP concurrency.  Each batch advances
    `current_batch_index` by 1 atomically via `mark_batch_completed`
    inside `_sweep_one_batch`, so the parallel workers naturally pick
    DIFFERENT batches without coordination.

    Re-arms quickly after exceptions so a single bad batch doesn't take
    down the broadcaster.

    Phase 26.31 (long-haul stability rewrite):
      The previous design did `fut.result(timeout=120)` per future.  When
      stock batches legitimately took >120s (which is normal for slow
      cold-cache passes — 100 symbols × multi-provider cascade × option
      chain fetch easily exceeds 2 minutes), every reap timed out.  Two
      timeouts per cycle × 2 cycles = 4 timeouts, which tripped the
      watchdog every 8 minutes, even though batches WERE completing in
      the background.  The watchdog then rebuilt the pool with
      `cancel_futures=True`, throwing away half-done work and starting
      the cycle over.  Hence the user-visible 4-minute "lockup" pattern.

      The new design:
        - Submit batches to a long-lived pool with a generous size.
        - Attach a `done_callback` to each future that records the
          ACTUAL completion timestamp (regardless of whether we're
          currently waiting on it).
        - The main loop self-throttles by checking in-flight count
          before submitting more work.  No more reap-with-timeout.
        - The watchdog fires ONLY if zero futures have completed for
          the entire stall window (10 minutes by default).  Slow but
          progressing scans are no longer mis-classified as wedged.
        - When the watchdog DOES fire, we shutdown(wait=False) without
          cancel_futures so any in-flight work that's about to finish
          completes naturally instead of being abandoned.
        - Adds a `batch completed` INFO log so the console shows
          forward progress; the user can confirm at a glance that the
          scanner is alive.
    """
    log.info(
        'snapshot scan loop started (stocks_limit=%d crypto_limit=%d parallel=%d)',
        _BATCH_LIMIT_STOCKS, _BATCH_LIMIT_CRYPTO, _PARALLEL_WORKERS,
    )

    # Build a dedicated thread pool.  Sized to (parallel * 2) so the
    # pool has a couple of spare slots for transient overlap, but the
    # `_MAX_IN_FLIGHT` ceiling below enforces the ACTUAL concurrency
    # at exactly `_PARALLEL_WORKERS` (2 by default) — same as the
    # pre-Phase-26.31 behavior.  This is critical: each batch fires
    # multi-provider HTTP cascades and the downstream Stooq/yfinance
    # pools serialize through their own as_completed(timeout=20s)
    # gates.  Letting 4+ batches run concurrently floods those gates
    # and deadlocks any concurrent /stocks/results request.
    if _PARALLEL_WORKERS > 1:
        from concurrent.futures import ThreadPoolExecutor as _Tpe

        _POOL_SIZE = _PARALLEL_WORKERS * 2

        def _new_pool() -> _Tpe:
            return _Tpe(
                max_workers=_POOL_SIZE,
                thread_name_prefix='snap-worker',
            )

        pool: _Tpe | None = _new_pool()
    else:
        pool = None
        _POOL_SIZE = 0

    # Completion bookkeeping.  Updated by `_on_done` from arbitrary
    # worker threads, so guarded by `_completion_lock`.  Counters live
    # inside one-element lists so the nested `_on_done` closure can
    # rebind without `nonlocal` gymnastics.
    _completion_lock = threading.Lock()
    _last_completion_mono = [time.monotonic()]
    _completions_total = [0]
    _failures_total = [0]
    _in_flight_ids: set = set()

    def _on_done(fut) -> None:
        try:
            exc = fut.exception()
        except Exception:  # noqa: BLE001 — defensive against cancelled futures
            exc = None
        with _completion_lock:
            _last_completion_mono[0] = time.monotonic()
            _in_flight_ids.discard(id(fut))
            if exc is None:
                _completions_total[0] += 1
                done_count = _completions_total[0]
            else:
                _failures_total[0] += 1
                done_count = None
        if exc is not None:
            log.warning(
                'snapshot batch failed: %s: %s',
                exc.__class__.__name__, exc,
            )
        else:
            # Heartbeat every batch.  Cheap (one log line per ~minute of
            # scoring) and gives the operator visible proof of progress.
            log.info(
                'snapshot batch completed (total_completed=%d)', done_count,
            )

    # Watchdog: a TRUE wedge is "no future has completed at all for
    # this many seconds".  Slow-but-progressing scans never trip this.
    _WATCHDOG_STALL_S = float(os.environ.get('SNAPSHOT_WATCHDOG_STALL_S', '600'))
    # Hard ceiling on outstanding work.  Match the pre-26.31 concurrency
    # exactly: never more than `_PARALLEL_WORKERS` (2) snap-workers
    # actively running batches.  The extra pool slots above are for
    # graceful drain during a watchdog rebuild, NOT for higher
    # concurrency.  Letting >2 batches run concurrently flooded the
    # downstream Stooq/yfinance sub-pools (each batch holds 20s+ as
    # the cascade times out) and deadlocked any concurrent
    # /stocks/results request.
    _MAX_IN_FLIGHT = _PARALLEL_WORKERS if pool is not None else 0
    # How long to pause between submit-and-check passes when the pool
    # is saturated (no available slots).  Short enough that we don't
    # idle visibly when capacity frees up, long enough not to busy-loop.
    _SATURATED_POLL_S = 5.0

    while True:
        for market in ('stocks', 'crypto'):
            try:
                if pool is not None:
                    workers = _PARALLEL_WORKERS if market == 'stocks' else max(1, _PARALLEL_WORKERS - 1)
                else:
                    workers = 0

                if pool is not None and workers > 0:
                    # Don't pile up unbounded work: only submit up to the
                    # number of free slots we have right now.
                    with _completion_lock:
                        in_flight_now = len(_in_flight_ids)
                    available_slots = max(0, _MAX_IN_FLIGHT - in_flight_now)
                    submit_count = min(workers, available_slots)

                    for _ in range(submit_count):
                        fut = pool.submit(_sweep_one_batch, market)
                        with _completion_lock:
                            _in_flight_ids.add(id(fut))
                        fut.add_done_callback(_on_done)

                    if submit_count < workers:
                        # Pool is saturated — give it a beat to drain
                        # before the next loop iteration tries again.
                        # This is not an error condition.
                        log.debug(
                            'snapshot pool saturated (in_flight=%d max=%d) — '
                            'waiting %.1fs before next submission',
                            in_flight_now, _MAX_IN_FLIGHT, _SATURATED_POLL_S,
                        )
                        time.sleep(_SATURATED_POLL_S)

                    # True-wedge detection: only fires if NO future has
                    # completed (success OR failure) for the entire stall
                    # window.  We do NOT cancel in-flight futures when
                    # rebuilding — they're daemon threads and either
                    # finish naturally (great) or leak until process exit
                    # (acceptable; the OS reaps the sockets eventually).
                    with _completion_lock:
                        stall_s = time.monotonic() - _last_completion_mono[0]
                        in_flight_now2 = len(_in_flight_ids)
                    if stall_s >= _WATCHDOG_STALL_S and in_flight_now2 > 0:
                        log.warning(
                            'snapshot watchdog: no batch completion in %.0fs '
                            '(in_flight=%d) — rebuilding worker pool',
                            stall_s, in_flight_now2,
                        )
                        try:
                            # wait=False so the call returns immediately;
                            # NO cancel_futures so in-flight batches that
                            # are about to complete still finish.  The
                            # callbacks on those futures will still fire
                            # against the new world (they just discard
                            # from `_in_flight_ids` — which has been
                            # cleared — so the .discard is a no-op).
                            pool.shutdown(wait=False)
                            # Phase 26.60: register the abandoned pool
                            # so the reaper can drop our reference once
                            # its blocked workers exit naturally.  This
                            # prevents unbounded thread accumulation
                            # across many watchdog rebuilds.
                            with _ABANDONED_SNAP_POOLS_LOCK:
                                _ABANDONED_SNAP_POOLS.append(
                                    (time.monotonic(), pool),
                                )
                        except Exception:  # noqa: BLE001
                            pass
                        # Opportunistic reap while we're already in the
                        # watchdog path — drops any prior pools whose
                        # workers finally released.
                        try:
                            _reap_abandoned_snap_pools()
                        except Exception:  # noqa: BLE001
                            pass
                        pool = _new_pool()
                        with _completion_lock:
                            _in_flight_ids.clear()
                            _last_completion_mono[0] = time.monotonic()
                else:
                    # Sequential path (parallel disabled): just sweep one
                    # batch synchronously and update the completion clock
                    # so the watchdog stays armed for the parallel path.
                    _sweep_one_batch(market)
                    with _completion_lock:
                        _last_completion_mono[0] = time.monotonic()
                        _completions_total[0] += 1
                    log.info(
                        'snapshot batch completed (total_completed=%d)',
                        _completions_total[0],
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning('snapshot loop sweep failed (market=%s): %s', market, exc)
                time.sleep(_LOOP_FAILURE_BACKOFF)
                continue
            time.sleep(_INTER_BATCH_SLEEP)
        time.sleep(_INTER_MARKET_SLEEP)


def start_scan_loop() -> None:
    """Idempotent.  Spawns the background scan loop on first call; subsequent
    calls are no-ops.  Called from app startup.

    In production (`start.bat` / `start.sh` run uvicorn WITHOUT --reload) this
    module is imported exactly once in a single process and the loop runs
    in that process's background thread.

    In dev (`uvicorn ... --reload`) uvicorn forks a `watchfiles` reloader
    parent process that *also* imports the app module to discover watch
    paths.  We skip the loop in the parent so we don't end up with two
    competing scan loops fighting over the same batch counter; the worker
    child is detected by having a non-init parent PID.  We deliberately
    use a SOFT check (env var + a heuristic) rather than something brittle
    like `multiprocessing.parent_process()` so the production-no-reload
    path always launches the loop.
    """
    global _loop_started
    # `WATCHFILES_FORCE_POLLING` is unconditionally set by uvicorn's reloader.
    # When present, the *parent* (reloader) process has it; the worker child
    # inherits it too.  Better discriminator: uvicorn's reloader sets
    # `UVICORN_RELOAD` to "true" only in the parent.
    in_reload_parent = (
        os.environ.get('UVICORN_RELOAD_PARENT') == '1'
        or os.environ.get('UVICORN_RELOAD_WATCHER') == '1'
    )
    if in_reload_parent:
        log.info('snapshot scan loop skipped (we are the --reload watcher parent)')
        return
    # Phase 26.31: don't launch the live scan loop under pytest.  The test
    # suite uses TestClient(app) which triggers the FastAPI lifespan and
    # thus this function.  A live scan loop pegs the test machine with
    # real provider HTTP calls and contends with the test's own
    # /stocks/results request, causing slow tests at best and timeouts at
    # worst.  Tests that NEED the scan loop running can set
    # SNAPSHOT_FORCE_SCAN_LOOP=1 (none currently do).
    #
    # Detection: PYTEST_CURRENT_TEST is set by pytest exactly when a
    # test is being collected/run.  Also check for the top-level
    # `pytest` module — but NOT substring matches like `_pytesttester`
    # (numpy's internal test runner), which would false-positive under
    # uvicorn since numpy is imported by pandas at app startup.
    import sys as _sys
    in_pytest = (
        'PYTEST_CURRENT_TEST' in os.environ
        or 'pytest' in _sys.modules
        or '_pytest' in _sys.modules
    )
    force = os.environ.get('SNAPSHOT_FORCE_SCAN_LOOP') == '1'
    if in_pytest and not force:
        log.info('snapshot scan loop skipped (pytest detected; set SNAPSHOT_FORCE_SCAN_LOOP=1 to override)')
        return
    with _loop_started_lock:
        if _loop_started:
            return
        _loop_started = True
    t = threading.Thread(target=_scan_loop_target, name='snapshot-scanner', daemon=True)
    t.start()
    log.info('snapshot scan loop thread launched (pid=%d)', os.getpid())


# ---------------------------------------------------------------------------
# Manual refresh / cache management (used by /stock/{sym}/refresh etc.)
# ---------------------------------------------------------------------------
def invalidate_symbol(market: str, symbol: str) -> bool:
    """Drop a symbol from the snapshot so the next sweep re-scores it from
    scratch.  Returns True if the symbol was actually present.
    """
    with _locks.get(market, _locks['stocks']):
        bucket = _snapshot.get(market) or {}
        if symbol in bucket:
            bucket.pop(symbol, None)
            _snapshot_meta.setdefault(market, {})['rows_scored'] = len(bucket)
            # Phase 26.37: removing a row changes the order — invalidate
            # the sorted-view cache so the next /api/scan/snapshot poll
            # doesn't serve a stale list that still contains this sym.
            _invalidate_sorted_view(market)
            return True
    return False


def clear_snapshot(market: str | None = None) -> None:
    """Wipe the snapshot for one market (or both)."""
    targets = [market] if market else ['stocks', 'crypto']
    for m in targets:
        with _locks.get(m, _locks['stocks']):
            _snapshot[m] = {}
            meta = _snapshot_meta.setdefault(m, {})
            meta['rows_scored'] = 0
            meta['current_batch_index'] = 0
            # Phase 26.37: hard-invalidate the sorted view; also drop the
            # cached entry outright so memory doesn't linger pointing at
            # stale rows (clear is rare; we can afford the explicit pop).
            _invalidate_sorted_view(m)
            _sorted_view_cache.pop(m, None)
    # Drop the thin-row LRU too — stale entries would otherwise leak
    # across clear/refill cycles (tests rely on this).
    _thin_cache_reset()
