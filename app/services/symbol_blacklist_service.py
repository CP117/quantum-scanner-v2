"""
Known-bad symbol blacklist.

Phase 24: ~15-20% of any NASDAQ universe is delisted equities, units,
warrants, rights, and merger placeholders that NO data provider will
ever return useful quotes for (e.g. `AABBW`, `ZZZZ.U`, `XYZ-WT`).
Before this module the scanner re-tried each of those symbols every
10 minutes — wasting ~2,000 yfinance/Yahoo round-trips per sweep on
symbols that will never resolve.

Algorithm
---------
Each time a symbol fails to fetch:
  1. Increment its failure counter for *today's UTC date*.
  2. If the symbol has now failed on >= `THRESHOLD_DAYS` distinct days,
     promote it to the blacklist (`blacklisted_at` populated).
  3. Blacklisted symbols are pruned from the active scan batches in
     `result_store.get_results_batch()`.

The "distinct days" requirement (rather than raw failure count) is
critical: a single hour of provider outage shouldn't permanently
remove every symbol from the scanner.  Symbols only graduate to the
blacklist after persistent failures across at least 3 separate UTC
days.

Storage
-------
Single small JSON file at `data/known_bad_symbols.json` — atomic
write, in-memory cache, idempotent reload on every startup.

Public API
----------
  record_failure(symbol) -> None     # called from scoring fail paths
  record_success(symbol) -> None     # clears failure history on recovery
  is_blacklisted(symbol) -> bool     # cheap O(1) check
  filter_blacklisted(symbols) -> list[str]   # drop blacklisted entries
  list_blacklisted() -> list[dict]   # for the diagnostic UI
  unblock(symbol) -> bool            # manual override (user-facing)
  stats() -> dict                    # for /system/status
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger('app.symbol_blacklist')

_DATA_PATH = Path(__file__).resolve().parent.parent.parent / 'data' / 'known_bad_symbols.json'
_THRESHOLD_DAYS = int(os.environ.get('BLACKLIST_THRESHOLD_DAYS', '3'))
_THRESHOLD_FAILURES_PER_DAY = int(os.environ.get('BLACKLIST_THRESHOLD_FAILURES_PER_DAY', '2'))
# When file growth is unbounded the json read/write costs add up.  Cap
# total entries (LRU pruning of oldest 'first_failure_at') so the file
# stays under ~500 KB even after months of operation.
_MAX_ENTRIES = int(os.environ.get('BLACKLIST_MAX_ENTRIES', '5000'))

_lock = threading.RLock()
_loaded = False
# Structure:
#   {
#     'SYMBOL': {
#       'failure_days': ['2026-05-21', '2026-05-22', ...],
#       'last_failure_at': '2026-05-23T...',
#       'first_failure_at': '2026-05-21T...',
#       'failure_count_total': 7,
#       'blacklisted_at': '2026-05-23T...'  (optional)
#     }, ...
#   }
_entries: dict[str, dict] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today_iso() -> str:
    return _utcnow().date().isoformat()


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        _loaded = True
        if not _DATA_PATH.exists():
            return
        try:
            payload = json.loads(_DATA_PATH.read_text(encoding='utf-8'))
            entries = payload.get('entries') or {}
            if isinstance(entries, dict):
                _entries.update(entries)
            log.info('symbol_blacklist: loaded %d entries (%d blacklisted)',
                     len(_entries),
                     sum(1 for v in _entries.values() if v.get('blacklisted_at')))
        except Exception as exc:  # noqa: BLE001
            log.warning('symbol_blacklist: failed to load %s: %s', _DATA_PATH, exc)


def _persist() -> None:
    """Atomic write of the current cache to disk.  Called from the same
    code paths that mutate `_entries`; assumes `_lock` is held."""
    try:
        _DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        # LRU prune if oversized.  Sort by `last_failure_at` ascending,
        # drop the oldest.
        if len(_entries) > _MAX_ENTRIES:
            sorted_keys = sorted(
                _entries.keys(),
                key=lambda k: _entries[k].get('last_failure_at', ''),
            )
            to_drop = sorted_keys[: len(_entries) - _MAX_ENTRIES]
            for k in to_drop:
                _entries.pop(k, None)
        tmp = _DATA_PATH.with_suffix('.json.tmp')
        tmp.write_text(
            json.dumps({'version': 1, 'entries': _entries}),
            encoding='utf-8',
        )
        os.replace(tmp, _DATA_PATH)
    except Exception as exc:  # noqa: BLE001
        log.debug('symbol_blacklist: persist failed: %s', exc)


def record_failure(symbol: str) -> bool:
    """Note that `symbol` failed to fetch.  Returns True iff this call
    *promoted* it to the blacklist (so the caller can log loudly the
    first time it's blacklisted)."""
    sym = (symbol or '').strip().upper()
    if not sym:
        return False
    _ensure_loaded()
    now_iso = _utcnow().isoformat()
    today = _today_iso()
    promoted = False
    with _lock:
        e = _entries.setdefault(sym, {
            'failure_days': [],
            'first_failure_at': now_iso,
            'last_failure_at': now_iso,
            'failure_count_total': 0,
        })
        e['last_failure_at'] = now_iso
        e['failure_count_total'] = int(e.get('failure_count_total', 0)) + 1
        days = e.get('failure_days') or []
        if today not in days:
            days.append(today)
            # Keep only the most recent 30 days so the list doesn't
            # grow forever on partially-flaky symbols.
            if len(days) > 30:
                days = days[-30:]
            e['failure_days'] = days
        # Promote to blacklist when distinct-day failure count exceeds threshold.
        if (
            not e.get('blacklisted_at')
            and len(e['failure_days']) >= _THRESHOLD_DAYS
        ):
            e['blacklisted_at'] = now_iso
            promoted = True
            log.info(
                'symbol_blacklist: promoted %s after %d distinct failure days (total failures: %d)',
                sym, len(e['failure_days']), e['failure_count_total'],
            )
        _persist()
    return promoted


def record_success(symbol: str) -> None:
    """Symbol fetched OK — clear its failure history if any.

    This is the recovery path: a symbol that was failing yesterday but
    started returning data today should be removed from the bad list
    so future flakiness doesn't accumulate forever.
    """
    sym = (symbol or '').strip().upper()
    if not sym:
        return
    _ensure_loaded()
    with _lock:
        if sym in _entries:
            del _entries[sym]
            _persist()


def is_blacklisted(symbol: str) -> bool:
    sym = (symbol or '').strip().upper()
    if not sym:
        return False
    _ensure_loaded()
    e = _entries.get(sym)
    return bool(e and e.get('blacklisted_at'))


def filter_blacklisted(symbols: Iterable[str]) -> list[str]:
    """Return only the symbols that are NOT currently blacklisted.

    Preserves input ordering; cheap O(n).
    """
    _ensure_loaded()
    # Snapshot the read-side under the lock to avoid mid-iteration mutation
    # surprises if another thread is recording failures concurrently.
    with _lock:
        blocked = {k for k, v in _entries.items() if v.get('blacklisted_at')}
    if not blocked:
        return [s.upper() for s in symbols if s]
    return [s for s in (sym.upper() for sym in symbols if sym) if s not in blocked]


def list_blacklisted() -> list[dict]:
    """Diagnostic list of currently-blacklisted symbols, sorted by
    most-recently blacklisted first.  Each dict carries the symbol,
    failure counts, and timestamps so the UI can render them in a table.
    """
    _ensure_loaded()
    with _lock:
        rows = []
        for sym, e in _entries.items():
            if not e.get('blacklisted_at'):
                continue
            rows.append({
                'symbol': sym,
                'blacklisted_at': e['blacklisted_at'],
                'first_failure_at': e.get('first_failure_at'),
                'last_failure_at': e.get('last_failure_at'),
                'failure_count_total': e.get('failure_count_total', 0),
                'failure_days': e.get('failure_days', []),
            })
    rows.sort(key=lambda r: r['blacklisted_at'] or '', reverse=True)
    return rows


def unblock(symbol: str) -> bool:
    """Manual user-facing override.  Drops a symbol from the blacklist
    AND resets its failure counters so it gets a clean slate."""
    sym = (symbol or '').strip().upper()
    if not sym:
        return False
    _ensure_loaded()
    with _lock:
        if sym in _entries:
            del _entries[sym]
            _persist()
            log.info('symbol_blacklist: %s manually unblocked', sym)
            return True
    return False


def stats() -> dict:
    _ensure_loaded()
    with _lock:
        total = len(_entries)
        blacklisted = sum(1 for v in _entries.values() if v.get('blacklisted_at'))
    return {
        'tracked': total,
        'blacklisted': blacklisted,
        'threshold_days': _THRESHOLD_DAYS,
        'max_entries': _MAX_ENTRIES,
    }
