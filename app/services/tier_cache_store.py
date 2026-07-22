"""
Tier Cache Store — Phase 28
============================

Provides tiered caching for the scanner:

  Tier 1 / Tier 2  — in-memory LRU dict (fast, always hot)
  Tier 3           — disk-backed LRU: metadata + last score in a per-letter
                     JSON shard, structured like the existing quote_cache shards

The module is intentionally lightweight.  Scoring rows are large; we only
cache the fields that the tier manager and promotion engine need for Tier 3
(symbol, score, change_pct, volume, avg_volume, last_price, prev_close,
last_scored_at).  Full factor breakdowns stay in snapshot_store for Tier 1/2.

Public API
----------
    ``save_tier3_summary(symbol, summary: dict)``
    ``get_tier3_summary(symbol) -> dict | None``
    ``save_tier2_row(symbol, row: dict)``
    ``get_tier2_row(symbol) -> dict | None``
    ``clear_tier3_cache()``
    ``clear_tier2_cache()``
    ``cache_status() -> dict``
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

log = logging.getLogger('app.tier_cache_store')

# ---------------------------------------------------------------------------
# Tier 2 — in-memory LRU (bounded)
# ---------------------------------------------------------------------------
_T2_MAX = int(os.environ.get('TIER2_CACHE_MAX', '1500'))  # rows
_t2_cache: 'OrderedDict[str, dict]' = OrderedDict()
_t2_lock = threading.Lock()


def save_tier2_row(symbol: str, row: dict) -> None:
    """Upsert a full scored row into the Tier 2 in-memory LRU cache."""
    key = symbol.upper()
    with _t2_lock:
        if key in _t2_cache:
            _t2_cache.move_to_end(key)
        _t2_cache[key] = row
        while len(_t2_cache) > _T2_MAX:
            _t2_cache.popitem(last=False)


def get_tier2_row(symbol: str) -> Optional[dict]:
    """Return a previously cached Tier 2 row, or None."""
    key = symbol.upper()
    with _t2_lock:
        row = _t2_cache.get(key)
        if row is not None:
            _t2_cache.move_to_end(key)
        return row


def clear_tier2_cache() -> None:
    """Drop all Tier 2 in-memory rows."""
    with _t2_lock:
        _t2_cache.clear()
    log.info('tier_cache_store: Tier 2 in-memory cache cleared')


def get_tier2_size() -> int:
    with _t2_lock:
        return len(_t2_cache)


# ---------------------------------------------------------------------------
# Tier 3 — disk-backed sharded JSON (per-letter, like quote_cache)
# ---------------------------------------------------------------------------

def _tier3_dir() -> Path:
    """Resolve the Tier 3 cache directory."""
    try:
        from app.config import settings
        d = settings.tier_3_cache_dir
    except Exception:  # noqa: BLE001
        d = 'data/tier3_cache'
    p = Path(d)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent.parent / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _shard_key(symbol: str) -> str:
    """Map a symbol to a one-letter shard bucket (A–Z or '_')."""
    s = (symbol or '_').upper()
    first = s[0] if s else '_'
    return first if first.isalpha() else '_'


# In-memory shard cache: letter → {'dirty': bool, 'data': {symbol: summary}}
_t3_shards: dict[str, dict] = {}
_t3_shards_lock = threading.Lock()
_t3_flush_interval_s = float(os.environ.get('TIER3_FLUSH_INTERVAL_S', '30.0'))


def _load_shard(letter: str) -> dict:
    """Load or return cached shard for *letter*."""
    with _t3_shards_lock:
        if letter in _t3_shards:
            return _t3_shards[letter]['data']

    fp = _tier3_dir() / f'{letter}.json'
    data: dict = {}
    try:
        if fp.exists():
            data = json.loads(fp.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        data = {}
    with _t3_shards_lock:
        _t3_shards[letter] = {'dirty': False, 'data': data}
    return data


def save_tier3_summary(symbol: str, summary: dict) -> None:
    """Persist a lightweight Tier 3 scoring summary to the disk shard."""
    key = symbol.upper()
    letter = _shard_key(key)
    data = _load_shard(letter)
    summary = dict(summary)
    summary['_saved_at'] = time.time()
    with _t3_shards_lock:
        _t3_shards[letter]['data'][key] = summary
        _t3_shards[letter]['dirty'] = True


def get_tier3_summary(symbol: str) -> Optional[dict]:
    """Return a Tier 3 scoring summary from disk cache, or None."""
    key = symbol.upper()
    letter = _shard_key(key)
    data = _load_shard(letter)
    with _t3_shards_lock:
        return data.get(key)


def clear_tier3_cache() -> None:
    """Drop all Tier 3 disk shards and in-memory state."""
    with _t3_shards_lock:
        _t3_shards.clear()
    try:
        d = _tier3_dir()
        for fp in d.glob('*.json'):
            try:
                fp.unlink()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    log.info('tier_cache_store: Tier 3 disk cache cleared')


def _flush_dirty_shards() -> int:
    """Write dirty shards to disk.  Returns count of shards flushed."""
    flushed = 0
    with _t3_shards_lock:
        dirty = [(k, v) for k, v in _t3_shards.items() if v.get('dirty')]

    d = _tier3_dir()
    for letter, shard in dirty:
        try:
            fp = d / f'{letter}.json'
            with _t3_shards_lock:
                data_copy = dict(shard['data'])
            fp.write_text(json.dumps(data_copy, separators=(',', ':')), encoding='utf-8')
            with _t3_shards_lock:
                _t3_shards[letter]['dirty'] = False
            flushed += 1
        except Exception:  # noqa: BLE001
            log.debug('tier_cache_store: failed to flush shard %s', letter, exc_info=True)
    return flushed


def _flush_loop() -> None:
    """Background daemon: flush dirty Tier 3 shards every _t3_flush_interval_s."""
    while True:
        time.sleep(_t3_flush_interval_s)
        try:
            n = _flush_dirty_shards()
            if n:
                log.debug('tier_cache_store: flushed %d Tier 3 shards to disk', n)
        except Exception:  # noqa: BLE001
            log.debug('tier_cache_store: flush loop error', exc_info=True)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def cache_status() -> dict:
    """Return cache health telemetry."""
    with _t2_lock:
        t2_size = len(_t2_cache)
    with _t3_shards_lock:
        t3_shards_loaded = len(_t3_shards)
        t3_total = sum(len(v['data']) for v in _t3_shards.values())
        t3_dirty = sum(1 for v in _t3_shards.values() if v.get('dirty'))
    return {
        'tier_2_rows_cached': t2_size,
        'tier_2_max': _T2_MAX,
        'tier_3_shards_loaded': t3_shards_loaded,
        'tier_3_symbols_cached': t3_total,
        'tier_3_dirty_shards': t3_dirty,
    }


# ---------------------------------------------------------------------------
# Module init
# ---------------------------------------------------------------------------
_initialized = False
_init_lock = threading.Lock()


def initialize() -> None:
    """Start the Tier 3 disk-flush background thread.  Idempotent."""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        _initialized = True
    t = threading.Thread(target=_flush_loop, name='tier3-cache-flusher', daemon=True)
    t.start()
    log.info('tier_cache_store: disk-flush thread started')
