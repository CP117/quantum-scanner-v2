
from __future__ import annotations

import logging
from typing import Any

from app.services.batch_service import get_batch_slice, get_total_batches
from app.services.scanner_metrics_registry import project_scanner_metrics, SCANNER_METRICS
from app.services.scanner_presets import apply_filters, resolve_filters, sort_rows
from app.services.scoring_service import score_symbol_rows
from app.services.status_service import (
    mark_failure,
    mark_success,
    record_last_known_good_serve,
    record_lkg_batch_stored,
)
from app.services.universe_service import get_universe
from app.utils.normalize import normalize_result_row, assert_contract_complete, REQUIRED_RESULT_KEYS
from app.utils.time import utcnow_iso, freshness_label_from_age, age_seconds_from_iso

log = logging.getLogger('app.results')

# Phase 20: LRU-cap the last-known-good map.  Previously this grew unbounded
# (492 batches × 25 rows × ~10 KB each = ~120 MB at full sweep) and the dict
# scan + memory pressure was a contributor to the late-sweep slowdown the
# user reported.  We keep the most recent N batches per (market, batch_index)
# eviction order.  100 is plenty for fallback purposes — older batches will
# get re-populated within ~25 seconds anyway as the snapshot loop wraps.
from collections import OrderedDict

_LAST_GOOD_MAX_ENTRIES = 200  # ~100 stock batches + ~100 crypto batches headroom
_LAST_GOOD_BY_BATCH: "OrderedDict[tuple[str, int], dict]" = OrderedDict()


def _record_last_good(key: tuple[str, int], envelope: dict) -> None:
    """Insert/refresh an LKG entry, evicting the oldest if we exceed the cap."""
    if key in _LAST_GOOD_BY_BATCH:
        # Move to the most-recent end.
        _LAST_GOOD_BY_BATCH.move_to_end(key)
    _LAST_GOOD_BY_BATCH[key] = envelope
    while len(_LAST_GOOD_BY_BATCH) > _LAST_GOOD_MAX_ENTRIES:
        _LAST_GOOD_BY_BATCH.popitem(last=False)


NUMERIC_FILTER_PARAMS = (
    'min_score', 'max_exit_risk', 'min_exit_risk',
    'min_institutional_confluence', 'max_institutional_confluence',
    'min_options_positioning', 'max_options_positioning',
    'min_trend_volume_delta', 'max_trend_volume_delta',
    'min_iob_score', 'max_iob_distance_pct', 'min_iob_confidence',
    'min_dark_pool_proxy', 'max_print_distance_pct', 'min_print_memory_score',
)

ENUM_FILTER_PARAMS = (
    'directions', 'tiers', 'exit_flags',
    'institutional_bias_in', 'options_bias_in', 'pin_risk_in',
    'trend_volume_delta_bucket_in', 'iob_state_in', 'iob_bias_in',
    'dark_pool_bias_in', 'pinning_effect_in',
)


def _attach_scanner_metrics(row: dict) -> dict:
    """Project the registry-defined flat metrics onto a normalized row.

    This is the single source of truth for what the filter/sort engine sees.
    """
    row['scanner_metrics'] = project_scanner_metrics(row)
    return row


def _mark_rows_stale_ok(rows: list[dict]) -> list[dict]:
    """Re-stamp freshness/state for last-known-good rows about to be served."""
    out: list[dict] = []
    for original in rows:
        row = dict(original)
        age = age_seconds_from_iso(row.get('as_of_utc'))
        row['age_seconds'] = age
        row['freshness_label'] = freshness_label_from_age(age)
        row['stale'] = True
        row['state'] = 'stale-ok'
        # Don't override `data_source` (cache/yfinance/inferred etc) — just
        # tag the envelope so the UI can show a clear LKG badge.
        row['lkg_fallback'] = True
        out.append(row)
    return out


def get_results_batch(
    batch: int,
    limit: int,
    preset: str | None = None,
    direction: str | None = None,
    tier: str | None = None,
    min_score: float | None = None,
    max_exit_risk: float | None = None,
    exit_flag: str | None = None,
    market: str | None = 'stocks',
    sort_by: str | None = None,
    sort_dir: str | None = 'desc',
    extra_filters: dict[str, Any] | None = None,
) -> dict:
    market = market or 'stocks'
    universe = get_universe(market)
    universe_size = len(universe)
    total_batches = max(1, get_total_batches(limit, market))
    current_batch = batch % total_batches
    filters = resolve_filters(preset, direction, tier, min_score, market=market)
    if max_exit_risk is not None:
        filters['max_exit_risk'] = float(max_exit_risk)
    if exit_flag:
        filters['exit_flags'] = [exit_flag]
    if extra_filters:
        for key, value in extra_filters.items():
            if value is None or value == '':
                continue
            filters[key] = value

    try:
        slice_rows = get_batch_slice(current_batch, limit, market)
        # Phase 24: drop persistently-failing symbols from the slice BEFORE
        # we waste an HTTP round-trip on them.  See
        # `symbol_blacklist_service.py` — symbols that failed on 3+ distinct
        # UTC days get blacklisted; an active scan loop on a 12k-stock
        # universe was wasting ~15% of its time retrying ~2,000 delisted
        # warrants and units that NO provider will ever resolve.
        from app.services.symbol_blacklist_service import filter_blacklisted, record_failure, record_success
        original_count = len(slice_rows)
        if original_count:
            allowed = set(filter_blacklisted(r.get('symbol') for r in slice_rows))
            slice_rows = [r for r in slice_rows if (r.get('symbol') or '').upper() in allowed]
        dropped = original_count - len(slice_rows)
        if dropped:
            log.debug('batch_assemble blacklist drop market=%s batch=%s dropped=%d',
                      market, current_batch, dropped)
        log.info(
            'batch_assemble start market=%s batch=%s limit=%s symbols=%s',
            market, current_batch, limit,
            [r.get('symbol') for r in slice_rows[:3]],
        )
        scored_rows = score_symbol_rows(slice_rows)
        # Phase 24: post-scoring, recognise successes vs failures so the
        # blacklist tracker can promote persistently-broken symbols and
        # clear successful ones from any prior failure history.  Anything
        # that came back with a usable last_price counts as a success;
        # anything tagged `state=degraded` or `provider_outcome=unavailable`
        # counts as a failure.
        for row in scored_rows:
            sym = (row.get('symbol') or '').upper()
            if not sym:
                continue
            outcome = (row.get('provider_outcome') or '').lower()
            mkt_fb = ((row.get('factor_breakdown') or {}).get('market') or {})
            has_price = float(mkt_fb.get('last_price') or 0) > 0
            failed = outcome in ('unavailable', 'live_failed') or not has_price
            try:
                if failed:
                    record_failure(sym)
                else:
                    # Only clear failure history if the symbol *had* one;
                    # avoids touching disk for symbols that have always worked.
                    from app.services.symbol_blacklist_service import _entries as _bl_entries
                    if sym in _bl_entries:
                        record_success(sym)
            except Exception:
                pass
        normalized_rows: list[dict] = []
        for raw in scored_rows:
            n = normalize_result_row(raw)
            assert_contract_complete(n)
            normalized_rows.append(_attach_scanner_metrics(n))
        filtered_rows = apply_filters(normalized_rows, filters)
        # Sorting
        descending = (sort_dir or 'desc').lower() != 'asc'
        if sort_by:
            filtered_rows = sort_rows(filtered_rows, sort_by, descending)
        else:
            filtered_rows = sort_rows(filtered_rows, 'final_score', descending)
        envelope = {
            'batch': current_batch,
            'current_batch': current_batch,
            'total_batches': total_batches,
            'limit': limit,
            'total': universe_size,
            'scan_progress': {
                'batch_index': current_batch,
                'batch_size': limit,
                'slice_rows': len(slice_rows),
                'scored_rows': len(normalized_rows),
                'loaded_rows': len(filtered_rows),
                'universe_size': universe_size,
                'scan_state': 'ok' if filtered_rows else 'empty',
            },
            'filters': {**filters, 'market': market, 'sort_by': sort_by or 'final_score', 'sort_dir': 'desc' if descending else 'asc'},
            'results': filtered_rows,
            'state': 'ok' if filtered_rows else 'empty',
        }
        _record_last_good((market, current_batch), envelope)
        record_lkg_batch_stored(len(_LAST_GOOD_BY_BATCH))
        # Refresh the active-scan pool so the next pass upgrades the new top
        # names to real options-chain pulls.
        try:
            from app.services.active_scan_pool import update_pool_from_rows
            update_pool_from_rows(filtered_rows or normalized_rows, top_n=25)
        except Exception:
            pass
        mark_success()
        log.info(
            'batch_assemble ok market=%s batch=%s scored=%s filtered=%s',
            market, current_batch, len(normalized_rows), len(filtered_rows),
        )
        return envelope
    except Exception as exc:
        mark_failure(str(exc), failure_class='assembly')
        fallback = _LAST_GOOD_BY_BATCH.get((market, current_batch))
        # Log full stack trace for diagnostic purposes; the WARNING line
        # alone only carries the exception class name, which makes
        # background-loop failures impossible to root-cause.
        log.warning(
            'batch_assemble failed market=%s batch=%s reason=%s fallback=%s',
            market, current_batch, type(exc).__name__, 'lkg' if fallback else 'empty',
            exc_info=True,
        )
        if fallback:
            stale = dict(fallback)
            stale['state'] = 'stale-ok'
            stale['results'] = _mark_rows_stale_ok(fallback.get('results') or [])
            stale['fallback_reason'] = f'{type(exc).__name__}: {exc}'[:200]
            record_last_known_good_serve(len(_LAST_GOOD_BY_BATCH), stale['fallback_reason'])
            return stale
        return {
            'batch': current_batch,
            'current_batch': current_batch,
            'total_batches': total_batches,
            'limit': limit,
            'total': universe_size,
            'scan_progress': {
                'batch_index': current_batch,
                'batch_size': limit,
                'loaded_rows': 0,
                'universe_size': universe_size,
                'scan_state': 'degraded',
            },
            'filters': {**filters, 'market': market, 'sort_by': sort_by or 'final_score'},
            'results': [],
            'state': 'degraded',
        }


def get_last_known_good_count() -> int:
    return len(_LAST_GOOD_BY_BATCH)


def clear_last_known_good() -> None:
    _LAST_GOOD_BY_BATCH.clear()
