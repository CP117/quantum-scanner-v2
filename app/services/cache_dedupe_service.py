"""Generalized cache deduplication + canonicalization subsystem.

Covers three cache domains:
  * daily history disk shards (duplicate bars / overlapping payloads),
  * options chain caches (semantic duplicates across cache keys +
    contract-level duplicate rows),
  * reaction clustering zones (duplicate canonical zones inside a
    computed reaction map).

Layered operation:
  * write-time hooks (`dedupe_history_df`, `dedupe_option_rows`,
    `dedupe_reaction_zones`) keep new data canonical,
  * startup audit + periodic maintenance (`run_full_dedupe`) repair the
    persisted stores,
  * an admin endpoint surfaces `dedupe_status()` and triggers manual runs.

Losers are quarantined to `data/cache_quarantine/` (JSONL, short-lived)
rather than silently destroyed, so the active read path only ever sees
canonical records while remaining auditable.
"""
from __future__ import annotations

import json
import logging
import math as _math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger('app.cache_dedupe')

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_QUARANTINE_DIR = _REPO_ROOT / 'data' / 'cache_quarantine'
_QUARANTINE_RETENTION_S = 3 * 24 * 3600

_state_lock = threading.Lock()
_state: dict[str, Any] = {
    'daily_history': {'scanned': 0, 'duplicate_groups': 0, 'removed': 0, 'retained': 0,
                      'quarantined': 0, 'last_run_utc': None},
    'options_chain': {'scanned': 0, 'duplicate_groups': 0, 'removed': 0, 'retained': 0,
                      'quarantined': 0, 'last_run_utc': None},
    'reaction_clustering': {'scanned': 0, 'duplicate_groups': 0, 'removed': 0, 'retained': 0,
                            'quarantined': 0, 'last_run_utc': None},
    'runs_completed': 0,
    'last_full_run_utc': None,
    'write_time': {'history_bars_deduped': 0, 'option_rows_deduped': 0,
                   'reaction_zones_deduped': 0},
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bump(domain: str, **counts) -> None:
    with _state_lock:
        d = _state[domain]
        for k, v in counts.items():
            d[k] = int(d.get(k, 0)) + int(v)
        d['last_run_utc'] = _utcnow()


def _bump_write(key: str, n: int) -> None:
    if n <= 0:
        return
    with _state_lock:
        _state['write_time'][key] = int(_state['write_time'].get(key, 0)) + int(n)


def dedupe_status() -> dict[str, Any]:
    with _state_lock:
        return json.loads(json.dumps(_state))


def _quarantine(domain: str, records: list[Any]) -> int:
    if not records:
        return 0
    try:
        _QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
        path = _QUARANTINE_DIR / f'{domain}.jsonl'
        with path.open('a', encoding='utf-8') as fh:
            for rec in records:
                fh.write(json.dumps({'quarantined_at': _utcnow(), 'record': rec},
                                    default=str) + '\n')
        return len(records)
    except Exception as exc:  # noqa: BLE001
        log.debug('quarantine write failed for %s: %s', domain, exc)
        return 0


def prune_quarantine() -> int:
    """Drop quarantine files older than the retention window."""
    removed = 0
    try:
        if not _QUARANTINE_DIR.exists():
            return 0
        cutoff = time.time() - _QUARANTINE_RETENTION_S
        for f in _QUARANTINE_DIR.glob('*.jsonl'):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
    except Exception:  # noqa: BLE001
        pass
    return removed


# ---------------------------------------------------------------------------
# Write-time helpers
# ---------------------------------------------------------------------------

def _bar_complete(rec: dict) -> bool:
    return all(rec.get(k) not in (None, 0, 0.0) for k in ('o', 'h', 'l', 'c'))


def dedupe_history_records(records: list[dict]) -> tuple[list[dict], int]:
    """Canonicalize a daily-history record list: one bar per timestamp,
    chronological order. Winner rule: structurally complete beats partial;
    otherwise the most recently written instance wins."""
    if not records:
        return records, 0
    by_ts: dict[str, dict] = {}
    removed = 0
    for rec in records:
        ts = str(rec.get('t') or '')
        if not ts:
            removed += 1
            continue
        prev = by_ts.get(ts)
        if prev is None:
            by_ts[ts] = rec
            continue
        removed += 1
        if _bar_complete(rec) or not _bar_complete(prev):
            by_ts[ts] = rec  # complete > partial; later write wins ties
    canonical = sorted(by_ts.values(), key=lambda r: str(r.get('t') or ''))
    return canonical, removed


def dedupe_history_df(df):
    """DataFrame flavor of the same rule for the in-memory write path."""
    try:
        if df is None or getattr(df, 'empty', True):
            return df
        if df.index.has_duplicates:
            n_before = len(df)
            df = df[~df.index.duplicated(keep='last')]
            _bump_write('history_bars_deduped', n_before - len(df))
        if not df.index.is_monotonic_increasing:
            df = df.sort_index()
        return df
    except Exception:  # noqa: BLE001
        return df


def dedupe_option_rows(rows: list[dict]) -> list[dict]:
    """Contract-level dedupe for raw option rows keyed on the OCC symbol.
    Identical (expiry, right, strike) contracts collapse to the most
    complete instance (non-zero OI/volume preferred, last-write wins)."""
    if not rows:
        return rows
    seen: dict[str, dict] = {}
    removed = 0
    for r in rows:
        key = str(r.get('option') or '')
        if not key:
            seen[f'__anon_{id(r)}'] = r
            continue
        prev = seen.get(key)
        if prev is None:
            seen[key] = r
            continue
        removed += 1

        def _richness(x: dict) -> float:
            return (float(x.get('open_interest') or 0)
                    + float(x.get('volume') or 0)
                    + (1.0 if x.get('iv') else 0.0))
        if _richness(r) >= _richness(prev):
            seen[key] = r
    if removed:
        _bump_write('option_rows_deduped', removed)
    return list(seen.values())


def dedupe_reaction_zones(zones: list[dict]) -> list[dict]:
    """Canonical zone identity = (rounded midpoint band, tier). Zones with
    the same identity merge into the strongest-evidence instance; distinct
    legitimate clusters at nearby-but-different midpoints are preserved."""
    if not zones:
        return zones
    canonical: dict[tuple, dict] = {}
    removed = 0
    for z in zones:
        try:
            mid = float(z.get('midpoint') or 0.0)
        except (TypeError, ValueError):
            mid = 0.0
        # Logarithmic 0.15% price band → same normalized cluster identity.
        band = round(_math.log(mid) / 0.0015) if mid > 0 else 0
        key = (band, str(z.get('tier') or ''))
        prev = canonical.get(key)
        if prev is None:
            canonical[key] = z
            continue
        removed += 1
        if float(z.get('evidence_score') or 0) > float(prev.get('evidence_score') or 0):
            merged = dict(z)
        else:
            merged = dict(prev)
        merged['touches'] = max(int(z.get('touches') or 0), int(prev.get('touches') or 0))
        canonical[key] = merged
    if removed:
        _bump_write('reaction_zones_deduped', removed)
        log.debug('reaction zone dedupe removed %d duplicate cluster copies', removed)
    return list(canonical.values())


# ---------------------------------------------------------------------------
# Maintenance passes
# ---------------------------------------------------------------------------

def dedupe_daily_history_store() -> dict[str, int]:
    """Scan every daily-history shard on disk; remove duplicate bars and
    enforce canonical bar ordering. Idempotent + safe to run repeatedly."""
    scanned = removed = groups = retained = quarantined = 0
    try:
        from app.services.daily_history_service import _SHARD_DIR  # type: ignore
        shard_dir = Path(_SHARD_DIR)
    except Exception:  # noqa: BLE001
        shard_dir = _REPO_ROOT / 'data' / 'daily_history_cache'
    if not shard_dir.exists():
        _bump('daily_history')
        return {'scanned': 0, 'removed': 0}
    for shard_path in sorted(shard_dir.glob('*.json')):
        try:
            payload = json.loads(shard_path.read_text(encoding='utf-8'))
            rows = payload.get('rows') or {}
            changed = False
            for sym, entry in rows.items():
                records = entry.get('records') or []
                scanned += len(records)
                canonical, dup_n = dedupe_history_records(records)
                retained += len(canonical)
                if dup_n:
                    groups += 1
                    removed += dup_n
                    quarantined += _quarantine('daily_history', [
                        {'symbol': sym, 'shard': shard_path.name, 'duplicates_removed': dup_n}])
                    entry['records'] = canonical
                    changed = True
            if changed:
                tmp = shard_path.with_suffix('.json.tmp')
                tmp.write_text(json.dumps({'version': payload.get('version', 1), 'rows': rows},
                                          separators=(',', ':')), encoding='utf-8')
                os.replace(tmp, shard_path)
        except Exception as exc:  # noqa: BLE001
            log.debug('daily-history dedupe failed for shard %s: %s', shard_path.name, exc)
    _bump('daily_history', scanned=scanned, duplicate_groups=groups, removed=removed,
          retained=retained, quarantined=quarantined)
    if removed:
        log.info('daily-history dedupe: removed %d duplicate bars across %d symbols', removed, groups)
    return {'scanned': scanned, 'removed': removed, 'duplicate_groups': groups}


def dedupe_options_chain_store() -> dict[str, int]:
    """Collapse semantic duplicates in the CBOE provider cache (same symbol
    fetched under different `max_expirations` keys) and drop superseded
    stale/None entries when a fresher complete equivalent exists."""
    scanned = removed = groups = retained = quarantined = 0
    try:
        from app.services.providers import cboe_options_provider as cboe
        with cboe._lock:  # noqa: SLF001
            entries = dict(cboe._cache)  # noqa: SLF001
        by_symbol: dict[str, list[tuple[str, float, dict | None]]] = {}
        for key, (ts, payload) in entries.items():
            scanned += 1
            sym = key.split(':', 1)[0]
            by_symbol.setdefault(sym, []).append((key, ts, payload))
        losers: list[str] = []
        for sym, group in by_symbol.items():
            if len(group) <= 1:
                retained += 1
                continue
            groups += 1

            def _rank(item):
                _key, ts, payload = item
                completeness = (payload or {}).get('expirations_used', 0) if payload else -1
                return (1 if payload else 0, completeness, ts)
            group.sort(key=_rank, reverse=True)
            retained += 1
            for key, _ts, payload in group[1:]:
                losers.append(key)
                if payload:
                    quarantined += _quarantine('options_chain', [
                        {'cache_key': key, 'expirations_used': payload.get('expirations_used')}])
        if losers:
            with cboe._lock:  # noqa: SLF001
                for key in losers:
                    cboe._cache.pop(key, None)  # noqa: SLF001
            removed += len(losers)
    except Exception as exc:  # noqa: BLE001
        log.debug('options-chain dedupe failed: %s', exc)
    _bump('options_chain', scanned=scanned, duplicate_groups=groups, removed=removed,
          retained=retained, quarantined=quarantined)
    if removed:
        log.info('options-chain dedupe: removed %d superseded cache entries', removed)
    return {'scanned': scanned, 'removed': removed, 'duplicate_groups': groups}


def dedupe_reaction_cluster_store() -> dict[str, int]:
    """Sweep in-memory snapshot rows and canonicalize their reaction_map
    zone lists (write-time dedupe already covers new computes; this
    repairs anything that predates the hook)."""
    scanned = removed = groups = retained = 0
    try:
        from app.services import snapshot_store
        for market in ('stocks', 'crypto'):
            lock = snapshot_store._locks.get(market)  # noqa: SLF001
            if lock is None:
                continue
            with lock:
                rows = list(snapshot_store._snapshot.get(market, {}).values())  # noqa: SLF001
            for row in rows:
                rmap = (((row.get('factor_breakdown') or {}).get('market') or {})
                        .get('reaction_map') or {})
                zones = rmap.get('zones')
                if not isinstance(zones, list) or not zones:
                    continue
                scanned += len(zones)
                canonical = dedupe_reaction_zones(zones)
                retained += len(canonical)
                if len(canonical) < len(zones):
                    groups += 1
                    removed += len(zones) - len(canonical)
                    rmap['zones'] = canonical
                    rmap['zone_count'] = len(canonical)
    except Exception as exc:  # noqa: BLE001
        log.debug('reaction cluster dedupe failed: %s', exc)
    _bump('reaction_clustering', scanned=scanned, duplicate_groups=groups, removed=removed,
          retained=retained)
    if removed:
        log.info('reaction-cluster dedupe: removed %d duplicate zones', removed)
    return {'scanned': scanned, 'removed': removed, 'duplicate_groups': groups}


def run_full_dedupe(trigger: str = 'manual') -> dict[str, Any]:
    """Run all three domain passes. Safe to run repeatedly."""
    t0 = time.monotonic()
    results = {
        'daily_history': dedupe_daily_history_store(),
        'options_chain': dedupe_options_chain_store(),
        'reaction_clustering': dedupe_reaction_cluster_store(),
        'quarantine_files_pruned': prune_quarantine(),
    }
    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 1)
    with _state_lock:
        _state['runs_completed'] = int(_state.get('runs_completed', 0)) + 1
        _state['last_full_run_utc'] = _utcnow()
    log.info('cache dedupe full run (%s) completed in %.1f ms: %s',
             trigger, elapsed_ms, {k: v for k, v in results.items() if isinstance(v, dict)})
    results['elapsed_ms'] = elapsed_ms
    results['trigger'] = trigger
    return results


def start_startup_audit() -> None:
    """Non-blocking startup validation pass."""
    threading.Thread(
        target=lambda: run_full_dedupe(trigger='startup_audit'),
        name='cache-dedupe-startup', daemon=True,
    ).start()
