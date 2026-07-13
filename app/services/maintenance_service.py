"""
Periodic maintenance worker for long-running installs (10+ day runs).

Without this, a few internal structures accumulate state forever:

  - `_stats` counters in providers/base.py, options_chain_service, daily_history,
    reaction_clustering: integer calls/hits/misses/errors. After ~10 days at
    a 12k-symbol-per-20-min sweep cadence, these grow into the tens of millions
    and the UI display becomes hard to scan even though Python ints don't
    overflow. We *rotate* them periodically: preserve the last_*_utc fields
    and reset the call/hit/miss/error counters to 0 so the UI shows
    "since last rotation" rather than "since process start".

  - `regulatory.db filings + awards` tables: SEC insider filings + USAspending
    awards accumulate forever. The scoring layer already ignores anything
    >15 days old, so anything older is dead weight on the DB. We hard-prune
    rows older than `REGULATORY_RETENTION_DAYS` (default 180).

  - `saved_predictions.db` predictions table: resolved predictions older
    than `PREDICTION_RETENTION_DAYS` (default 90) get hard-pruned. Open /
    unresolved predictions are NEVER touched.

  - `recent_fetch_error_summary` + `failure_classes` Counter in status_service:
    Counter grows by one key per distinct failure class observed. We rotate
    these alongside provider counters.

  - `last_known_good.serves_total`: monotonically increasing. We snapshot
    the previous value into `serves_total_lifetime` and reset the live
    counter at rotation time.

Run cadence:
  - Counter rotation runs every `COUNTER_ROTATE_INTERVAL_SECONDS`
    (default 6 hours).
  - DB pruning runs every `DB_PRUNE_INTERVAL_SECONDS` (default 24 hours).

All operations are no-ops if the underlying file/DB doesn't exist yet, so
the maintenance loop is safe to start before any data has been written.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

log = logging.getLogger('app.maintenance')


# ---------------------------------------------------------------------------
# Tunables (env-overridable)
# ---------------------------------------------------------------------------
COUNTER_ROTATE_INTERVAL_SECONDS = int(
    os.environ.get('COUNTER_ROTATE_INTERVAL_SECONDS', str(6 * 3600))
)
DB_PRUNE_INTERVAL_SECONDS = int(
    os.environ.get('DB_PRUNE_INTERVAL_SECONDS', str(24 * 3600))
)
CACHE_DEDUPE_INTERVAL_SECONDS = int(
    os.environ.get('CACHE_DEDUPE_INTERVAL_SECONDS', str(6 * 3600))
)
REGULATORY_RETENTION_DAYS = int(os.environ.get('REGULATORY_RETENTION_DAYS', '180'))
PREDICTION_RETENTION_DAYS = int(os.environ.get('PREDICTION_RETENTION_DAYS', '90'))

# Resolve DB paths the same way the regulatory/prediction modules do.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent.parent  # /app/app/services -> /app
_REG_DB_CANDIDATES = [
    Path('data/regulatory.db'),
    _REPO_ROOT / 'data' / 'regulatory.db',
    _REPO_ROOT / 'app' / 'data' / 'regulatory.db',
]
_PRED_DB_CANDIDATES = [
    Path('data/saved_predictions.db'),
    _REPO_ROOT / 'data' / 'saved_predictions.db',
    _REPO_ROOT / 'app' / 'data' / 'saved_predictions.db',
]


def _pick(paths: list[Path]) -> Path | None:
    for p in paths:
        try:
            if p.exists() and p.is_file():
                return p
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Last-run state (queryable by /api/admin/maintenance)
# ---------------------------------------------------------------------------
_state_lock = Lock()
_state: dict[str, Any] = {
    'counter_rotations': 0,
    'last_counter_rotation_utc': None,
    'last_counter_rotation_summary': None,
    'db_prunes': 0,
    'last_db_prune_utc': None,
    'last_db_prune_summary': None,
    'started_at_utc': None,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_counter_rotation(summary: dict) -> None:
    with _state_lock:
        _state['counter_rotations'] += 1
        _state['last_counter_rotation_utc'] = _utc_now_iso()
        _state['last_counter_rotation_summary'] = summary


def _record_db_prune(summary: dict) -> None:
    with _state_lock:
        _state['db_prunes'] += 1
        _state['last_db_prune_utc'] = _utc_now_iso()
        _state['last_db_prune_summary'] = summary


def maintenance_status() -> dict:
    """Read-only snapshot for /api/admin/maintenance."""
    with _state_lock:
        return {
            **{k: v for k, v in _state.items()},
            'counter_rotate_interval_seconds': COUNTER_ROTATE_INTERVAL_SECONDS,
            'db_prune_interval_seconds': DB_PRUNE_INTERVAL_SECONDS,
            'regulatory_retention_days': REGULATORY_RETENTION_DAYS,
            'prediction_retention_days': PREDICTION_RETENTION_DAYS,
            'regulatory_db_path': str(_pick(_REG_DB_CANDIDATES) or _REG_DB_CANDIDATES[0]),
            'prediction_db_path': str(_pick(_PRED_DB_CANDIDATES) or _PRED_DB_CANDIDATES[0]),
        }


# ---------------------------------------------------------------------------
# Counter rotation
# ---------------------------------------------------------------------------

def rotate_provider_counters() -> dict:
    """Reset growable integer counters on every per-provider stats dict
    while preserving last-success/last-error timestamps and circuit-breaker
    state. Returns a per-provider summary of the previous values for the UI.

    Touches the in-memory dicts directly:
      - app.services.providers.base._stats
      - app.services.options_chain_service._stats
      - app.services.daily_history_service._stats
      - app.services.reaction_clustering_service._stats
    """
    summary: dict[str, dict] = {}
    rotated_counter_keys = (
        # Generic integer counters - safe to zero across all stat dicts.
        'calls', 'hits', 'misses', 'errors', 'timeouts', 'rate_limits',
        'attempts', 'hits_real', 'cache_hits', 'cooldown_skips',
        'no_options_skips', 'fetch_error_skips', 'throttle_skips',
        'no_options_unique_symbols', 'rate_limited', 'computed',
        'unavailable', 'prefetch_queued', 'prefetch_dropped',
        'shards_flushed', 'disk_loaded_rows', 'shards_migrated',
        'hits_cryptocompare', 'flush_seconds_total',
        # circuit_trip_count intentionally NOT rotated - keep historical tally
    )
    # We will NOT zero: last_error, last_*_utc, circuit_state,
    # consecutive_failures, cb_threshold, cb_cooldown_seconds, etc.

    try:
        from app.services.providers import base as _base
        # Phase 26.16 / Tier 1.5: walk per-provider locks so we don't
        # block concurrent provider calls while rotating counters. We
        # briefly hold each provider's lock just long enough to snapshot
        # and zero its rotated keys.
        with _base._locks_table_lock:
            names = list(_base._stats.keys())
        prev = {}
        for name in names:
            lk = _base._lock_for(name)
            with lk:
                row = _base._stats.get(name)
                if row is None:
                    continue
                prev[name] = {k: row.get(k, 0) for k in rotated_counter_keys if k in row}
                for k in rotated_counter_keys:
                    if k in row and isinstance(row[k], (int, float)):
                        row[k] = 0 if isinstance(row[k], int) else 0.0
        summary['quote_providers'] = prev
    except Exception as exc:  # noqa: BLE001
        log.warning('rotate: quote-provider counters failed: %s', exc)
        summary['quote_providers'] = {'error': str(exc)}

    # Options chain stats
    try:
        from app.services import options_chain_service as _ocs
        if hasattr(_ocs, '_stats'):
            prev = {k: _ocs._stats[k] for k in rotated_counter_keys if k in _ocs._stats}
            for k in rotated_counter_keys:
                if k in _ocs._stats and isinstance(_ocs._stats[k], (int, float)):
                    _ocs._stats[k] = 0 if isinstance(_ocs._stats[k], int) else 0.0
            summary['options_chain'] = prev
    except Exception as exc:  # noqa: BLE001
        log.warning('rotate: options-chain counters failed: %s', exc)

    # Daily history stats
    try:
        from app.services import daily_history_service as _dhs
        if hasattr(_dhs, '_stats'):
            prev = {k: _dhs._stats[k] for k in rotated_counter_keys if k in _dhs._stats}
            for k in rotated_counter_keys:
                if k in _dhs._stats and isinstance(_dhs._stats[k], (int, float)):
                    _dhs._stats[k] = 0 if isinstance(_dhs._stats[k], int) else 0.0
            summary['daily_history'] = prev
    except Exception as exc:  # noqa: BLE001
        log.warning('rotate: daily-history counters failed: %s', exc)

    # Reaction clustering stats
    try:
        from app.services import reaction_clustering_service as _rcs
        if hasattr(_rcs, '_stats'):
            prev = {k: _rcs._stats[k] for k in rotated_counter_keys if k in _rcs._stats}
            for k in rotated_counter_keys:
                if k in _rcs._stats and isinstance(_rcs._stats[k], (int, float)):
                    _rcs._stats[k] = 0 if isinstance(_rcs._stats[k], int) else 0.0
            summary['reaction_clustering'] = prev
    except Exception as exc:  # noqa: BLE001
        log.warning('rotate: reaction-clustering counters failed: %s', exc)

    # status_service failure-class Counter + LKG serves_total
    try:
        from app.services import status_service as _ss
        with _ss._runtime_lock:
            prev_classes = dict(_ss._runtime['failure_classes'])
            prev_serves = int(_ss._runtime['last_known_good'].get('serves_total') or 0)
            _ss._runtime['failure_classes'].clear()
            # Keep lifetime counter in a separate field so the UI can show
            # both "since rotation" and "since process start".
            lifetime = int(_ss._runtime['last_known_good'].get('serves_total_lifetime') or 0)
            _ss._runtime['last_known_good']['serves_total_lifetime'] = lifetime + prev_serves
            _ss._runtime['last_known_good']['serves_total'] = 0
            summary['failure_classes'] = prev_classes
            summary['lkg_serves_rotated'] = prev_serves
    except Exception as exc:  # noqa: BLE001
        log.warning('rotate: status_service rotation failed: %s', exc)

    _record_counter_rotation(summary)
    log.info('counter rotation complete: %d provider buckets touched',
             len((summary.get('quote_providers') or {})))
    return summary


# ---------------------------------------------------------------------------
# Database pruning
# ---------------------------------------------------------------------------

def _prune_sqlite(
    db_path: Path,
    table: str,
    cutoff_iso: str,
    cutoff_column: str,
    extra_where: str = '',
) -> int:
    """Delete rows older than the cutoff. Returns the row count actually
    deleted. Safe to call on non-existent / empty DBs - returns 0.
    """
    if not db_path or not db_path.exists():
        return 0
    try:
        with sqlite3.connect(str(db_path)) as conn:
            # Synchronous=NORMAL for speed; this is a maintenance op, not
            # latency-critical.
            conn.execute('PRAGMA synchronous=NORMAL')
            where = f'{cutoff_column} < ?'
            if extra_where:
                where = f'{where} AND ({extra_where})'
            cur = conn.execute(
                f'SELECT COUNT(*) FROM {table} WHERE {where}',
                (cutoff_iso,),
            )
            (n_to_delete,) = cur.fetchone()
            if n_to_delete > 0:
                conn.execute(
                    f'DELETE FROM {table} WHERE {where}',
                    (cutoff_iso,),
                )
                conn.commit()
            return int(n_to_delete)
    except sqlite3.Error as exc:
        log.warning('prune: %s.%s failed: %s', db_path.name, table, exc)
        return 0


def prune_databases() -> dict:
    """Run the configured retention pruning across all known SQLite stores.

    Tables touched:
      - regulatory.db:
          * filings WHERE filing_date < (now - REGULATORY_RETENTION_DAYS)
          * awards WHERE action_date < (now - REGULATORY_RETENTION_DAYS)
      - saved_predictions.db:
          * saved_predictions WHERE resolved_at IS NOT NULL
            AND resolved_at < (now - PREDICTION_RETENTION_DAYS).
            Open predictions are NEVER pruned.

    Returns a summary the UI can render.
    """
    summary: dict[str, Any] = {}
    now = datetime.now(timezone.utc)

    # Regulatory.
    reg_cutoff = (now - timedelta(days=REGULATORY_RETENTION_DAYS)).date().isoformat()
    reg_db = _pick(_REG_DB_CANDIDATES)
    if reg_db:
        deleted_filings = _prune_sqlite(reg_db, 'filings', reg_cutoff, 'filing_date')
        deleted_awards = _prune_sqlite(reg_db, 'awards', reg_cutoff, 'action_date')
        summary['regulatory'] = {
            'db': str(reg_db),
            'cutoff_date': reg_cutoff,
            'filings_pruned': deleted_filings,
            'awards_pruned': deleted_awards,
        }
        # VACUUM if we deleted a non-trivial number of rows. VACUUM rebuilds
        # the DB file and reclaims disk; can be slow on big DBs so we only
        # do it after a big prune.
        if deleted_filings + deleted_awards > 1000:
            try:
                with sqlite3.connect(str(reg_db)) as conn:
                    conn.execute('VACUUM')
                summary['regulatory']['vacuumed'] = True
            except sqlite3.Error as exc:
                log.warning('VACUUM regulatory failed: %s', exc)
    else:
        summary['regulatory'] = {'db': None, 'note': 'regulatory.db not found'}

    # Saved predictions.
    pred_cutoff = (now - timedelta(days=PREDICTION_RETENTION_DAYS)).isoformat()
    pred_db = _pick(_PRED_DB_CANDIDATES)
    if pred_db:
        # Only prune resolved predictions (resolved_at IS NOT NULL).
        deleted_preds = _prune_sqlite(
            pred_db,
            'saved_predictions',
            pred_cutoff,
            'resolved_at',
            extra_where='resolved_at IS NOT NULL',
        )
        summary['predictions'] = {
            'db': str(pred_db),
            'cutoff_iso': pred_cutoff,
            'resolved_pruned': deleted_preds,
            'unresolved_kept': True,
        }
        if deleted_preds > 100:
            try:
                with sqlite3.connect(str(pred_db)) as conn:
                    conn.execute('VACUUM')
                summary['predictions']['vacuumed'] = True
            except sqlite3.Error as exc:
                log.warning('VACUUM predictions failed: %s', exc)
    else:
        summary['predictions'] = {'db': None, 'note': 'saved_predictions.db not found'}

    _record_db_prune(summary)
    log.info('db pruning complete: %s', summary)
    return summary


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------
_thread: threading.Thread | None = None
_thread_stop = threading.Event()


def _loop() -> None:
    log.info(
        'maintenance loop started: counter_rotate=%ds db_prune=%ds reg_retention=%dd pred_retention=%dd',
        COUNTER_ROTATE_INTERVAL_SECONDS, DB_PRUNE_INTERVAL_SECONDS,
        REGULATORY_RETENTION_DAYS, PREDICTION_RETENTION_DAYS,
    )
    last_counter_rotate = time.monotonic()
    last_db_prune = time.monotonic()
    last_cache_dedupe = time.monotonic()

    # First DB prune runs 5 minutes after startup so the user sees the
    # housekeeping kick in on day one of a fresh install (instead of waiting
    # the full 24 h cycle to see whether the loop is alive).
    first_db_prune_due = time.monotonic() + 300

    while not _thread_stop.is_set():
        try:
            now_mono = time.monotonic()
            if now_mono - last_counter_rotate >= COUNTER_ROTATE_INTERVAL_SECONDS:
                try:
                    rotate_provider_counters()
                except Exception as exc:  # noqa: BLE001
                    log.exception('counter rotation crashed: %s', exc)
                last_counter_rotate = now_mono

            # Periodic cache dedupe / canonicalization maintenance.
            if now_mono - last_cache_dedupe >= CACHE_DEDUPE_INTERVAL_SECONDS:
                try:
                    from app.services.cache_dedupe_service import run_full_dedupe
                    run_full_dedupe(trigger='periodic_maintenance')
                except Exception as exc:  # noqa: BLE001
                    log.exception('cache dedupe crashed: %s', exc)
                last_cache_dedupe = now_mono

            do_prune = (now_mono - last_db_prune >= DB_PRUNE_INTERVAL_SECONDS) or (
                first_db_prune_due and now_mono >= first_db_prune_due
            )
            if do_prune:
                try:
                    prune_databases()
                except Exception as exc:  # noqa: BLE001
                    log.exception('db prune crashed: %s', exc)
                last_db_prune = now_mono
                first_db_prune_due = 0  # consumed
        except Exception as exc:  # noqa: BLE001
            log.exception('maintenance loop tick crashed: %s', exc)
        # Sleep in short increments so a stop signal is honored promptly.
        _thread_stop.wait(30.0)


def start_maintenance_thread() -> None:
    """Idempotent: starts the background maintenance loop once."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread_stop.clear()
    with _state_lock:
        _state['started_at_utc'] = _utc_now_iso()
    _thread = threading.Thread(
        target=_loop,
        name='maintenance-loop',
        daemon=True,
    )
    _thread.start()


def stop_maintenance_thread() -> None:
    _thread_stop.set()


# ---------------------------------------------------------------------------
# Async-friendly wrappers for use from FastAPI routes
# ---------------------------------------------------------------------------

async def rotate_provider_counters_async() -> dict:
    return await asyncio.to_thread(rotate_provider_counters)


async def prune_databases_async() -> dict:
    return await asyncio.to_thread(prune_databases)
