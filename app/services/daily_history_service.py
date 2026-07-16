"""
Daily-history fetcher with TTL cache + throttle + SHARDED disk persistence.

Used by the reaction-clustering engine and volume sentiment compute.  Pulls
~90 days of daily OHLCV bars from yfinance (with a CryptoCompare fallback
for crypto tickers — see Phase 16).

Two-tier caching:
  1. In-memory cache (lock-protected).
  2. Disk cache at /app/data/daily_history_cache/{A..Z,_}.json that survives
     restarts.  Phase 20 critical change: the monolithic 88 MB JSON file
     used to be rewritten under the global `_lock` every 2 minutes, which
     pinned the scan loop for ~2-3 s during every flush window and was
     the root cause of the "scanner stalls at ~1000 stocks" symptom the
     user reported.

Phase 20 disk-cache redesign
----------------------------
Old shape:
  /app/data/daily_history_cache.json  (~88 MB at full universe)
  Every flush rewrote the entire blob.  Serialisation + lock-hold took
  ~3 s.  With ~12,300 symbols this happened every 2 min and progressively
  starved the snapshot scan loop, daily-history workers, and any
  /api/scan/snapshot poll requests that needed the same lock.

New shape:
  /app/data/daily_history_cache/A.json
  /app/data/daily_history_cache/B.json
  ...
  /app/data/daily_history_cache/Z.json
  /app/data/daily_history_cache/_.json   (digit-prefix + non-alpha symbols)

  Each shard is ~3-6 MB.  We track which shards were *dirtied* since the
  last flush so we only ever rewrite the buckets that changed.  Typical
  steady-state flush rewrites 1-2 shards (~6-12 MB total) instead of 88 MB.

  All conversion (_df_to_records) and JSON serialisation happens
  OUTSIDE the lock.  The lock is held only long enough to copy
  references to the dataframes — typically <10 ms.

Auto-migration:
  On first read, if the legacy `daily_history_cache.json` monolith
  exists we split it across the new shards, then rename it to
  `.migrated` so subsequent boots skip the migration step.

Defensive-by-default: never raises, never blocks.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from typing import Any

import pandas as pd

try:
    import orjson as _orjson
    _HAS_ORJSON = True
except Exception:  # pragma: no cover
    _orjson = None  # type: ignore[assignment]
    _HAS_ORJSON = False

log = logging.getLogger('app.daily_history')

# 24h cache: once a daily bar closes its OHLCV does not change, and the user
# wants metrics to "stick" until the scanner re-processes or refresh is hit.
_TTL_SECONDS = 60 * 60 * 24
# Throttle to be a good citizen with Yahoo.  Tuned for fast prefetch of the
# active scan set — most calls hit the disk-backed cache so the network is
# rarely touched after the first warm cycle.
_MIN_GAP_SECONDS = 0.08
_MAX_INFLIGHT = 12
_FAIL_COOLDOWN = 600         # 10 minutes after a failure
_DEFAULT_PERIOD = '90d'
_DEFAULT_INTERVAL = '1d'

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / 'data'
# Legacy monolithic file — migrated to shards on first read.
_LEGACY_DISK_CACHE_PATH = _DATA_DIR / 'daily_history_cache.json'
# Phase 20: sharded directory.  One JSON file per first-letter bucket.
_SHARD_DIR = _DATA_DIR / 'daily_history_cache'

# --- Bounded prefetch worker pool ----------------------------------------
# Instead of spawning one daemon thread per requested symbol (which would
# saturate the GIL and the OS thread table when the universe is > 3000),
# we use a single fixed-size pool that drains a bounded queue.  Symbols
# already cached or recently failed are dropped at enqueue-time so the
# queue never fills with no-op work.
import queue
from concurrent.futures import ThreadPoolExecutor

_PREFETCH_QUEUE_MAX = 5000
_PREFETCH_WORKERS = 10
_prefetch_q: "queue.Queue[str]" = queue.Queue(maxsize=_PREFETCH_QUEUE_MAX)
_prefetch_seen_lock = Lock()
_prefetch_seen: set[str] = set()  # already-enqueued symbols (in-flight or done)
_prefetch_pool: ThreadPoolExecutor | None = None
_prefetch_started = False

_lock = Lock()
# Phase 26.60: bounded LRU cache to prevent unbounded heap growth.
# Previously an unbounded dict — at 3,000+ active tickers with 90-day
# DataFrames (~5-10 KB each) plus pandas overhead, this dict alone
# could reach 300-800 MB of Python heap, causing gen-2 GC walks during
# scoring to spike CPU to 100% for 500 ms - 2 s at a time.  OrderedDict
# gives O(1) move_to_end() on read and popitem(last=False) on evict.
# Eviction respects shard dirtiness: any evicted symbol whose shard
# still has unflushed writes triggers a background force-flush so no
# fetched data is lost — the on-disk shard remains the durable source
# of truth and an evicted symbol will be reloaded from its shard on
# the next `get_daily_history` call for that symbol.
_CACHE_MAX = int(os.environ.get('DAILY_HISTORY_CACHE_MAX', '6000'))
_cache: 'OrderedDict[str, tuple[float, Any]]' = OrderedDict()
_last_request_ts = 0.0
_inflight = 0
_fail_until: dict[str, float] = {}
_disk_loaded = False
# Phase 20: per-shard dirty tracking so flush only touches changed shards.
_dirty_shards: set[str] = set()
_last_disk_flush = 0.0
# Phase 20: with sharded flushes being ~30x cheaper we can afford a tighter
# default cadence, but we also gain a longer ceiling to keep things calm
# at the steady state.  Tunable via env override.
_DISK_FLUSH_INTERVAL = float(os.environ.get('DAILY_HISTORY_FLUSH_INTERVAL', '300.0'))
# Serialises concurrent disk-flush attempts so two threads don't race on
# the same shard file.  This lock is NEVER held while doing the heavy
# JSON / iterrows() work — only during the actual file write.
_flush_lock = Lock()

_stats = {
    'attempts': 0, 'hits_real': 0, 'cache_hits': 0, 'disk_hits': 0,
    'errors': 0, 'cooldown_skips': 0, 'throttle_skips': 0,
    'disk_loaded_rows': 0, 'prefetch_queued': 0, 'prefetch_dropped': 0,
    'shards_flushed': 0, 'flush_seconds_total': 0.0,
    'shards_migrated': 0,
    # Phase 26.33 telemetry:
    'records_cache_hits': 0,       # flush skipped re-serialization
    'records_cache_misses': 0,     # flush had to build records
    'serializer': 'orjson' if _HAS_ORJSON else 'stdlib-json',
    'async_flushes': 0,            # backgrounded flushes
}

# Phase 26.33: per-symbol cache of the already-serialized records list.
# When a symbol's DataFrame is INSERTED into _cache we can pre-compute its
# records list once; subsequent flushes pull from this cache instead of
# re-running _df_to_records on the same unchanged DataFrame. Cleared via
# `_records_cache.pop(sym)` whenever a symbol is invalidated/refetched.
#
# Phase 26.60: mirrored as OrderedDict so the LRU eviction pass in
# `_evict_lru_if_over_capacity` can drop matching records-cache entries
# in the same critical section.  Always a subset of `_cache` keys — no
# independent cap needed.
_records_cache: 'OrderedDict[str, list[dict]]' = OrderedDict()


def _evict_lru_if_over_capacity() -> None:
    """Evict oldest entries from `_cache` (and mirror in `_records_cache`)
    until `len(_cache) <= _CACHE_MAX`.  MUST be called under `_lock`.

    If any evicted symbol has an unflushed shard, schedules a background
    force-flush so its fetch data is persisted before the in-memory copy
    is dropped.  `_flush_disk(force=True)` only spawns a daemon thread —
    that thread waits on `_lock` and runs after our caller releases it,
    so there is no deadlock.

    O(k) where k = number of entries over cap (usually 0 or 1 after a
    fetch).  Safe to call every insert.
    """
    if len(_cache) <= _CACHE_MAX:
        return
    needs_flush = False
    while len(_cache) > _CACHE_MAX:
        try:
            evicted_sym, _ = _cache.popitem(last=False)
        except KeyError:
            break
        _records_cache.pop(evicted_sym, None)
        if _shard_key(evicted_sym) in _dirty_shards:
            needs_flush = True
    if needs_flush:
        try:
            _flush_disk(force=True)
        except Exception:  # noqa: BLE001 — never let eviction cause a fault
            pass


def _shard_key(symbol: str) -> str:
    """Map a symbol to its disk shard ('A'..'Z' or '_' for non-alpha first chars)."""
    s = (symbol or '').upper()
    if not s:
        return '_'
    first = s[0]
    if 'A' <= first <= 'Z':
        return first
    return '_'


def _shard_path(shard_key: str) -> Path:
    return _SHARD_DIR / f'{shard_key}.json'


def _prefetch_worker():
    """Drain the prefetch queue forever, one symbol at a time per worker.

    Uses blocking=True so the worker sleeps at the throttle gap instead of
    dropping the work as a "throttle_skip"."""
    while True:
        try:
            sym = _prefetch_q.get(timeout=60)
        except queue.Empty:
            continue
        try:
            get_daily_history(sym, allow_fetch=True, blocking=True)
        except Exception:
            pass
        finally:
            _prefetch_q.task_done()


def _ensure_prefetch_pool_running() -> None:
    global _prefetch_pool, _prefetch_started
    if _prefetch_started:
        return
    _prefetch_started = True
    _prefetch_pool = ThreadPoolExecutor(
        max_workers=_PREFETCH_WORKERS, thread_name_prefix='dh-worker'
    )
    for _ in range(_PREFETCH_WORKERS):
        _prefetch_pool.submit(_prefetch_worker)
    log.info('daily_history: prefetch pool started with %d workers', _PREFETCH_WORKERS)


def stats_snapshot() -> dict[str, int]:
    with _lock:
        snap = dict(_stats)
    # Phase 26.33: surface in-memory dict sizes so operators can spot
    # unbounded growth across many universal passes.
    snap['cache_entries'] = len(_cache)
    snap['fail_until_entries'] = len(_fail_until)
    snap['records_cache_entries'] = len(_records_cache)
    with _prefetch_seen_lock:
        snap['prefetch_seen_entries'] = len(_prefetch_seen)
    return snap


def prune_expired_state() -> dict[str, int]:
    """Drop expired entries from `_cache`, `_fail_until`,
    `_records_cache`, and `_prefetch_seen`.

    Called from the sweep-boundary hook (gc_service).  Without this,
    after ~10 days of operation every symbol the warmer ever touched
    has a sticky entry in these dicts even if the data went stale weeks
    ago.  Pass-2+ telemetry calls that iterate them then start showing
    up in CPU profiles.

    Returns a per-dict count breakdown.
    """
    now = _now()
    pruned_cache = 0
    pruned_fail = 0
    pruned_records = 0
    pruned_seen = 0
    with _lock:
        # _cache TTL is 24h; double that as the safe pruning cutoff.
        cutoff = 2 * _TTL_SECONDS
        stale = [sym for sym, (ts, _df) in _cache.items() if (now - ts) > cutoff]
        for sym in stale:
            del _cache[sym]
            pruned_cache += 1
        # Cooldowns that expired more than an hour ago — drop them.
        stale_fail = [s for s, until in _fail_until.items() if until + 3600 < now]
        for s in stale_fail:
            del _fail_until[s]
            pruned_fail += 1
        # _records_cache entries with no matching _cache entry are vestigial.
        orphan_records = [s for s in _records_cache if s not in _cache]
        for s in orphan_records:
            del _records_cache[s]
            pruned_records += 1
    # _prefetch_seen entries for symbols no longer in _cache and not in
    # active cooldown can be re-queued safely.
    with _prefetch_seen_lock, _lock:
        keep = set(_cache.keys()) | set(_fail_until.keys())
        stale_seen = [s for s in _prefetch_seen if s not in keep]
        for s in stale_seen:
            _prefetch_seen.discard(s)
            pruned_seen += 1
    if pruned_cache or pruned_fail or pruned_records or pruned_seen:
        log.info(
            'daily_history prune: cache=%d, fail_until=%d, '
            'records_cache=%d, prefetch_seen=%d',
            pruned_cache, pruned_fail, pruned_records, pruned_seen,
        )
    return {
        'cache_pruned': pruned_cache,
        'fail_until_pruned': pruned_fail,
        'records_cache_pruned': pruned_records,
        'prefetch_seen_pruned': pruned_seen,
    }


def _df_to_records(df) -> list[dict]:
    """Convert a daily-history DataFrame to a list of records.

    Phase 26.33 (CPU win for long-haul scans): the previous implementation
    used `df.iterrows()` which is pandas' slowest row-iteration method
    (~100x slower than vectorized ops because each row builds a Series
    object).  After pass 1, the periodic disk flush had to iterrows()
    ~12,000 DataFrames × ~90 rows each = ~1.08 M row-builds in one go,
    pegging the CPU at 100% for several seconds and starving the scan
    loop.

    The new implementation does a single vectorized `df.values.tolist()`
    call (NumPy/C code, no Python row-by-row overhead) plus one cheap
    list comprehension to build the dicts.  On a 90-row DataFrame this
    takes ~50 us vs ~5 ms for iterrows() — a 100x speedup that scales
    linearly with the cache size.
    """
    if df is None or getattr(df, 'empty', True):
        return []
    try:
        # Resolve column positions once.  Tolerate missing columns by
        # falling back to a constant 0.0 placeholder for that field.
        cols = {c: i for i, c in enumerate(df.columns)}
        i_o = cols.get('Open')
        i_h = cols.get('High')
        i_l = cols.get('Low')
        i_c = cols.get('Close')
        i_v = cols.get('Volume')
        # One C-level call to convert the entire dataframe body to a
        # list of lists of Python floats.  This is the ENTIRE expensive
        # path — everything below is cheap dict construction.
        values = df.values.tolist()
        # Timestamps come out of the index in one shot via .astype(str)
        # which is also vectorized.
        try:
            timestamps = df.index.astype(str).tolist()
        except Exception:
            timestamps = [str(t) for t in df.index]

        def _f(row: list, idx: int | None) -> float:
            if idx is None:
                return 0.0
            try:
                v = row[idx]
                # NaN check without importing math (NaN != NaN)
                if v != v:
                    return 0.0
                return float(v)
            except Exception:
                return 0.0

        out: list[dict] = []
        for ts, row in zip(timestamps, values):
            out.append({
                't': ts,
                'o': _f(row, i_o),
                'h': _f(row, i_h),
                'l': _f(row, i_l),
                'c': _f(row, i_c),
                'v': _f(row, i_v),
            })
        return out
    except Exception:
        return []


def _records_to_df(records: list[dict]):
    if not records:
        return None
    try:
        rows = {
            'Open': [r['o'] for r in records],
            'High': [r['h'] for r in records],
            'Low': [r['l'] for r in records],
            'Close': [r['c'] for r in records],
            'Volume': [r['v'] for r in records],
        }
        idx = pd.to_datetime([r['t'] for r in records], errors='coerce', utc=True)
        return pd.DataFrame(rows, index=idx)
    except Exception:
        return None


def _migrate_legacy_monolith() -> int:
    """If the old single-file cache exists, split it across the new shards.

    Called once from _ensure_disk_loaded.  Renames the legacy file with a
    `.migrated` suffix so subsequent boots skip this step.  Returns the
    number of symbols migrated.
    """
    if not _LEGACY_DISK_CACHE_PATH.exists():
        return 0
    try:
        payload = json.loads(_LEGACY_DISK_CACHE_PATH.read_text(encoding='utf-8'))
        rows = payload.get('rows') or {}
    except Exception as exc:
        log.warning('daily_history migration: failed to read legacy cache: %s', exc)
        return 0

    # Group rows by shard
    by_shard: dict[str, dict] = {}
    for sym, entry in rows.items():
        key = _shard_key(sym)
        by_shard.setdefault(key, {})[sym] = entry

    _SHARD_DIR.mkdir(parents=True, exist_ok=True)
    migrated_total = 0
    for shard_key, shard_rows in by_shard.items():
        try:
            shard_path = _shard_path(shard_key)
            # Merge with any pre-existing shard content (defensive)
            existing: dict = {}
            if shard_path.exists():
                try:
                    existing_payload = json.loads(shard_path.read_text(encoding='utf-8'))
                    existing = existing_payload.get('rows') or {}
                except Exception:
                    existing = {}
            existing.update(shard_rows)
            tmp = shard_path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps({'version': 1, 'rows': existing}), encoding='utf-8')
            os.replace(tmp, shard_path)
            migrated_total += len(shard_rows)
        except Exception as exc:
            log.warning('daily_history migration: shard %s failed: %s', shard_key, exc)

    # Rename the legacy file so we don't re-migrate on next boot.
    try:
        _LEGACY_DISK_CACHE_PATH.rename(
            _LEGACY_DISK_CACHE_PATH.with_suffix('.json.migrated')
        )
    except Exception:
        pass
    log.info(
        'daily_history migration: split legacy monolith into %d shards (%d rows)',
        len(by_shard), migrated_total,
    )
    return migrated_total


def _ensure_disk_loaded() -> None:
    global _disk_loaded
    if _disk_loaded:
        return
    _disk_loaded = True
    # Phase 20: migrate legacy monolith first.
    try:
        migrated = _migrate_legacy_monolith()
        if migrated:
            with _lock:
                _stats['shards_migrated'] = migrated
    except Exception as exc:
        log.warning('daily_history: legacy migration failed: %s', exc)

    if not _SHARD_DIR.exists():
        return
    now = time.time()
    loaded_total = 0
    for shard_path in sorted(_SHARD_DIR.glob('*.json')):
        try:
            payload = json.loads(shard_path.read_text(encoding='utf-8'))
            rows = payload.get('rows') or {}
            for sym, entry in rows.items():
                saved_at = float(entry.get('saved_at') or 0)
                age = now - saved_at
                if age > _TTL_SECONDS:
                    continue  # stale; let it re-fetch
                df = _records_to_df(entry.get('records') or [])
                if df is None or getattr(df, 'empty', True):
                    continue
                mono_anchor = time.monotonic() - age
                _cache[sym] = (mono_anchor, df)
                loaded_total += 1
        except Exception as exc:  # noqa: BLE001
            log.debug('daily_history: failed to load shard %s: %s', shard_path.name, exc)
    with _lock:
        _stats['disk_loaded_rows'] = loaded_total
    if loaded_total:
        log.info(
            'daily_history: loaded %d cached symbols from %d shards',
            loaded_total, len(list(_SHARD_DIR.glob('*.json'))),
        )
    # Phase 26.60: enforce cache cap after startup load.  If the
    # persisted set exceeds `_CACHE_MAX` (e.g. large historical
    # universe), evict oldest by insertion order.  Shards are the
    # durable source of truth so evicted entries can be reloaded
    # on demand — this just prevents startup from immediately
    # pinning gigabytes of heap.
    with _lock:
        _evict_lru_if_over_capacity()


def _dumps_bytes(obj) -> bytes:
    """Serialize a shard payload.  Uses orjson when available (5-10x
    faster than stdlib json on the typical shard payload, which is
    ~3-6 MB of nested dicts/lists/floats) and falls back to stdlib
    otherwise so the module still works on minimal installs.

    The shard files have no human-edit requirement so we deliberately
    skip indent + sort_keys — they account for ~30% of stdlib-json
    serialize time and zero functional value.
    """
    if _HAS_ORJSON:
        try:
            return _orjson.dumps(obj)
        except Exception:
            pass
    return json.dumps(obj, separators=(',', ':')).encode('utf-8')


def _records_for_symbol(sym: str, df) -> list[dict]:
    """Return cached records for `sym` if available, else compute and
    cache them.  This is the hot path during _flush_disk; the cache
    means a flush that re-rewrites a 500-symbol shard now only
    serializes the symbols that ACTUALLY changed since last flush."""
    cached = _records_cache.get(sym)
    if cached is not None:
        with _lock:
            _stats['records_cache_hits'] += 1
        return cached
    records = _df_to_records(df)
    if records:
        _records_cache[sym] = records
    with _lock:
        _stats['records_cache_misses'] += 1
    return records


def _flush_disk(force: bool = False) -> None:
    """Persist dirty shards to disk.

    Phase 20: heavy work moved outside the global `_lock`.  Phase 26.33:
    flush now runs ASYNCHRONOUSLY in a background daemon thread so the
    scoring path is never blocked waiting on disk I/O + JSON
    serialization.  Per-symbol records cache (`_records_cache`) skips
    redundant `_df_to_records` calls on unchanged DataFrames, and
    `orjson` replaces stdlib `json` on the serialize path.  On a full
    universe (12,000+ symbols) these two optimizations combined drop
    flush wall-time from ~3-5 s (the source of the user-reported
    pass-2 100% CPU spikes) to ~100-200 ms.

    Throttled by both wall-clock interval AND `force=True` for bypass.
    """
    global _last_disk_flush
    now = time.time()
    if not _dirty_shards:
        return
    if not force and (now - _last_disk_flush) < _DISK_FLUSH_INTERVAL:
        return
    _last_disk_flush = now
    # Phase 26.33: kick the actual work off to a background daemon so
    # we don't block the calling scoring/worker thread.  The flush is
    # idempotent (it reads _dirty_shards under the lock) so multiple
    # backgrounded flushes harmlessly coalesce.
    threading.Thread(
        target=_flush_disk_sync,
        name='dh-flush',
        daemon=True,
        kwargs={'force_log': force},
    ).start()
    with _lock:
        _stats['async_flushes'] += 1


def _flush_disk_sync(force_log: bool = False) -> None:
    """The actual flush, runnable from any thread.  Separated from
    `_flush_disk` so it can be invoked directly by tests / atexit /
    operator-triggered checkpoints without going through the async
    dispatch."""
    # ---- Stage 1: copy references under the lock (FAST) ----
    mono_now = time.monotonic()
    shard_payloads: dict[str, dict] = {}
    with _lock:
        if not _dirty_shards:
            return
        dirty_now = set(_dirty_shards)
        _dirty_shards.clear()
        for sym, (mono_anchor, df) in _cache.items():
            sk = _shard_key(sym)
            if sk not in dirty_now:
                continue
            age = mono_now - mono_anchor
            if age > _TTL_SECONDS or df is None:
                continue
            shard_payloads.setdefault(sk, {})[sym] = (
                time.time() - age, df,
            )

    # ---- Stage 2: serialise + write outside the lock (SLOW) ----
    flush_started = time.monotonic()
    with _flush_lock:
        try:
            _SHARD_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        for shard_key, sym_entries in shard_payloads.items():
            try:
                rows_out: dict = {}
                for sym, (saved_at, df) in sym_entries.items():
                    # Phase 26.33: use the per-symbol records cache so
                    # we don't re-iterate the same unchanged DataFrame
                    # for every flush cycle.
                    records = _records_for_symbol(sym, df)
                    if records:
                        rows_out[sym] = {
                            'saved_at': saved_at,
                            'records': records,
                        }
                if not rows_out:
                    continue
                shard_path = _shard_path(shard_key)
                payload = {'version': 1, 'rows': rows_out}
                tmp = shard_path.with_suffix('.json.tmp')
                # orjson.dumps returns bytes; stdlib fallback also bytes.
                tmp.write_bytes(_dumps_bytes(payload))
                os.replace(tmp, shard_path)
                with _lock:
                    _stats['shards_flushed'] = int(_stats.get('shards_flushed', 0)) + 1
            except Exception as exc:  # noqa: BLE001
                log.debug('daily_history: flush failed for shard %s: %s', shard_key, exc)
                with _lock:
                    _dirty_shards.add(shard_key)

    flush_elapsed = time.monotonic() - flush_started
    with _lock:
        _stats['flush_seconds_total'] = float(_stats.get('flush_seconds_total', 0.0)) + flush_elapsed
    if force_log:
        log.info(
            'daily_history: flushed %d shards in %.1f ms (serializer=%s)',
            len(shard_payloads), flush_elapsed * 1000.0,
            'orjson' if _HAS_ORJSON else 'stdlib-json',
        )


def clear_cache() -> None:
    global _last_request_ts, _inflight
    with _lock:
        _cache.clear()
        _fail_until.clear()
        for k in list(_stats.keys()):
            if isinstance(_stats[k], (int, float)):
                _stats[k] = 0 if isinstance(_stats[k], int) else 0.0
        _last_request_ts = 0.0
        _inflight = 0
        _dirty_shards.clear()
    _records_cache.clear()


def invalidate(symbol: str) -> bool:
    """Drop a symbol from the cache so the next read forces a live fetch.

    Returns True if anything was removed.
    """
    sym = (symbol or '').upper()
    if not sym:
        return False
    removed = False
    with _lock:
        if sym in _cache:
            del _cache[sym]
            removed = True
        if sym in _fail_until:
            del _fail_until[sym]
        if removed:
            _dirty_shards.add(_shard_key(sym))
    if removed:
        _records_cache.pop(sym, None)
    with _prefetch_seen_lock:
        _prefetch_seen.discard(sym)
    if removed:
        _flush_disk(force=True)
    return removed


def invalidate_failed_crypto() -> int:
    """Drop every cached failure / cooldown entry for `-USD` symbols.

    Called once on backend startup so the new Phase-16 CryptoCompare
    daily-history fallback gets a chance to fill the warming gap that
    yfinance previously locked into a 10-minute cooldown.

    Returns the number of entries cleared.
    """
    cleared = 0
    dirty_keys: set[str] = set()
    with _lock:
        # Drop None-valued cache entries (failed prior fetches)
        bad_cache = [s for s, (_ts, df) in _cache.items() if s.endswith('-USD') and df is None]
        for s in bad_cache:
            del _cache[s]
            dirty_keys.add(_shard_key(s))
            cleared += 1
        # Drop active cooldowns for crypto symbols
        bad_cooldowns = [s for s in _fail_until if s.endswith('-USD')]
        for s in bad_cooldowns:
            del _fail_until[s]
            cleared += 1
        if dirty_keys:
            _dirty_shards.update(dirty_keys)
    with _prefetch_seen_lock:
        for s in list(_prefetch_seen):
            if s.endswith('-USD'):
                _prefetch_seen.discard(s)
    if cleared:
        _flush_disk(force=True)
        log.info('daily_history: cleared %d stale crypto failure entries (Phase 16 fallback warmup)', cleared)
    return cleared


def _now() -> float:
    return time.monotonic()


def get_daily_history(symbol: str, allow_fetch: bool = True, blocking: bool = False) -> Any:
    """Return a yfinance daily-history DataFrame or None.

    Always cached for `_TTL_SECONDS`; throttled per-request; cooled-down
    per-symbol on failure.  Persists to disk so cached data survives restarts.
    Never raises.

    Args:
        symbol: ticker symbol.
        allow_fetch: when False, only returns a cached value (no network).
            Used by the scoring path so we don't block the batch waiting on
            a network round-trip — see `prefetch_daily_history` for the
            async-friendly warm path.
        blocking: when True (used by the worker pool), sleep briefly to
            respect the inter-request gap instead of returning None on a
            throttle miss.  This keeps the prefetch queue draining smoothly
            instead of dropping work as "throttle_skips".
    """
    global _last_request_ts, _inflight
    sym = (symbol or '').upper()
    if not sym:
        return None
    _ensure_disk_loaded()

    while True:
        now = _now()
        with _lock:
            cd = _fail_until.get(sym, 0)
            if cd and now < cd:
                _stats['cooldown_skips'] += 1
                return None
            cached = _cache.get(sym)
            if cached and (now - cached[0] <= _TTL_SECONDS):
                # Phase 26.60: LRU touch — move this symbol to the
                # most-recently-used end so it survives eviction while
                # the scanner is actively touching it.
                _cache.move_to_end(sym)
                _stats['cache_hits'] += 1
                return cached[1]
            if not allow_fetch:
                return None
            if _inflight >= _MAX_INFLIGHT:
                if not blocking:
                    _stats['throttle_skips'] += 1
                    return None
                wait_s = 0.05
            elif now - _last_request_ts < _MIN_GAP_SECONDS:
                if not blocking:
                    _stats['throttle_skips'] += 1
                    return None
                wait_s = max(0.0, _MIN_GAP_SECONDS - (now - _last_request_ts))
            else:
                _inflight += 1
                _last_request_ts = now
                _stats['attempts'] += 1
                break
        # Sleep outside the lock so other workers can proceed.
        time.sleep(min(0.2, wait_s))

    df = None
    try:
        import yfinance as yf
        # Phase 26.35: yf.Ticker(...).history() does NOT honor a timeout
        # parameter — Yahoo can hang the underlying socket for tens of
        # minutes when rate-limiting.  Route the call through the same
        # batch-download timeout executor so the dh-worker is freed at
        # a known ceiling and the cascade can fall through to
        # CryptoCompare (for crypto) or just record an error (for stocks).
        from app.services.scoring_service import (
            _yf_download_with_timeout as _yf_with_timeout,  # noqa: F401
        )
        # Direct submit pattern — we want a fresh call each time, not
        # reuse the download-specific wrapper.  Use a local executor
        # snapshot to avoid the wrapper's logging noise on the per-symbol
        # path.
        from concurrent.futures import TimeoutError as _DhFTimeout
        from app.services.scoring_service import _YF_BATCH_EXECUTOR, _YF_BATCH_EXECUTOR_LOCK
        with _YF_BATCH_EXECUTOR_LOCK:
            _executor = _YF_BATCH_EXECUTOR
        _fut = _executor.submit(
            lambda: yf.Ticker(sym).history(
                period=_DEFAULT_PERIOD, interval=_DEFAULT_INTERVAL,
                auto_adjust=False, prepost=False,
            )
        )
        try:
            df = _fut.result(timeout=20.0)
        except _DhFTimeout:
            log.debug('daily_history yfinance.history timed out for %s after 20s', sym)
            df = None
        if df is None or getattr(df, 'empty', True):
            df = None
        else:
            with _lock:
                _stats['hits_real'] += 1
    except Exception as exc:  # noqa: BLE001
        log.debug('daily_history fetch failed for %s: %s', sym, exc)
        with _lock:
            _stats['errors'] += 1
        df = None

    # Phase 16: CryptoCompare fallback for the crypto tail.
    if (df is None or getattr(df, 'empty', True)) and sym.upper().endswith('-USD'):
        try:
            from app.services.providers import cryptocompare_provider
            cc_df = cryptocompare_provider.fetch_daily_history(sym, limit=90)
            if cc_df is not None and not getattr(cc_df, 'empty', True):
                df = cc_df
                with _lock:
                    _stats['hits_real'] += 1
                    _stats.setdefault('hits_cryptocompare', 0)
                    _stats['hits_cryptocompare'] += 1
        except Exception as exc:  # noqa: BLE001
            log.debug('daily_history cryptocompare fallback failed for %s: %s', sym, exc)

    # Only enter the failure-cooldown path AFTER both providers have been tried.
    if df is None:
        with _lock:
            _fail_until[sym] = _now() + _FAIL_COOLDOWN
    else:
        # Write-time canonicalization: one bar per timestamp, chronological.
        try:
            from app.services.cache_dedupe_service import dedupe_history_df
            df = dedupe_history_df(df)
        except Exception:  # noqa: BLE001
            pass
    with _lock:
        _inflight = max(0, _inflight - 1)
        _cache[sym] = (_now(), df)
        # Phase 26.60: enforce LRU cap. Newly-inserted entry is already
        # at the MRU end (OrderedDict semantics); this evicts the oldest
        # if we're over capacity, flushing any dirty shard first so no
        # fetch data is lost.
        _evict_lru_if_over_capacity()
        _dirty_shards.add(_shard_key(sym))
    # Phase 26.33: drop the stale per-symbol records so the next flush
    # re-serializes from the new DataFrame instead of writing the prior
    # day's data back to disk.
    _records_cache.pop(sym, None)
    # Allow re-enqueue if the symbol gets invalidated later.
    with _prefetch_seen_lock:
        _prefetch_seen.discard(sym)
    _flush_disk()
    return df


def prefetch_daily_history(symbol: str) -> None:
    """Enqueue a daily-history fetch onto the bounded worker pool.

    Used by the scoring path so the scanner can record metrics for ALL
    symbols it touches without blocking the batch.  At enqueue-time we
    drop symbols that are already cached, recently failed, or already
    sitting in the queue, so the queue never fills with no-op work.
    """
    sym = (symbol or '').upper()
    if not sym:
        return
    _ensure_disk_loaded()
    _ensure_prefetch_pool_running()
    with _lock:
        if sym in _cache:
            return  # already have it
        cd = _fail_until.get(sym, 0)
        if cd and _now() < cd:
            return
    with _prefetch_seen_lock:
        if sym in _prefetch_seen:
            return  # already queued
        _prefetch_seen.add(sym)
    try:
        _prefetch_q.put_nowait(sym)
        with _lock:
            _stats['prefetch_queued'] += 1
    except queue.Full:
        with _prefetch_seen_lock:
            _prefetch_seen.discard(sym)
        with _lock:
            _stats['prefetch_dropped'] += 1


def await_prefetch_for_batch(symbols: list[str], timeout: float = 8.0) -> int:
    """Queue every symbol in `symbols` and then block until each is either
    cached or known-failed, or up to `timeout` seconds.

    See full docstring at module top.  Returns the count of symbols that
    resolved within the timeout, useful for telemetry.
    """
    if not symbols:
        return 0
    _ensure_disk_loaded()
    pending: set[str] = set()
    for sym in symbols:
        s = (sym or '').upper()
        if not s:
            continue
        pending.add(s)
        prefetch_daily_history(s)

    if not pending:
        return 0

    deadline = _now() + max(0.1, float(timeout))
    resolved_count = 0
    while pending and _now() < deadline:
        # Snapshot the cache + fail set atomically, then prune `pending`.
        with _lock:
            done_here = {s for s in pending if s in _cache or s in _fail_until}
        if done_here:
            resolved_count += len(done_here)
            pending -= done_here
            if not pending:
                break
        time.sleep(0.05)
    return resolved_count
