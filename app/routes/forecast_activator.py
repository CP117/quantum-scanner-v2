"""Dedicated Future Forecast Activator route.

Powers the per-row `Forecast` button in the scanner table. Returns a
normalized forecast payload that visibly incorporates short selling
pressure, predicted volume intensity and options-expiration context.

Debounced: per-symbol results are cached for a short TTL and concurrent
requests for the same symbol coalesce behind a per-symbol lock so
repeated clicks can't overload the pipeline or block the scanner feed.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, Query

log = logging.getLogger('app.forecast_activator')

router = APIRouter(prefix='/api/forecast', tags=['forecast'])

_CACHE_TTL_S = 30.0
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_symbol_locks: dict[str, threading.Lock] = {}
_symbol_locks_guard = threading.Lock()


def _lock_for(symbol: str) -> threading.Lock:
    with _symbol_locks_guard:
        lk = _symbol_locks.get(symbol)
        if lk is None:
            lk = threading.Lock()
            _symbol_locks[symbol] = lk
            if len(_symbol_locks) > 2048:
                _symbol_locks.clear()
                _symbol_locks[symbol] = lk
        return lk


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        return default if x != x else x
    except (TypeError, ValueError):
        return default


def _horizon_summary(blk: dict | None) -> dict | None:
    if not isinstance(blk, dict) or not blk:
        return None
    p_up = blk.get('p_up_ctx', blk.get('p_up_cf', blk.get('p_up')))
    p_up = _f(p_up, 0.5)
    return {
        'direction': 'Bullish' if p_up >= 0.55 else 'Bearish' if p_up <= 0.45 else 'Neutral',
        'p_up': round(p_up, 4),
        'p_up_base': round(_f(blk.get('p_up_cf', blk.get('p_up')), 0.5), 4),
        'drift_pct': round(_f(blk.get('drift_pct')), 4),
        'sigma_pct': round(_f(blk.get('sigma_pct')), 4),
        'directional_certainty': round(
            _f(blk.get('directional_certainty_ctx',
                       blk.get('directional_certainty_cf', blk.get('directional_certainty')))), 4),
        'squeeze_probability': round(_f(blk.get('squeeze_probability')), 4),
        'volatility_event_probability': round(_f(blk.get('volatility_event_probability')), 4),
        'tier': blk.get('tier', 'fast'),
    }


def _build_payload(symbol: str, market: str) -> dict[str, Any]:
    from app.services.snapshot_store import lookup_snapshot_row
    from app.services.forecast_context import (
        apply_forecast_context, build_forecast_context, summarize_forecast,
    )

    row = lookup_snapshot_row(symbol, market)
    if row is None:
        return {
            'symbol': symbol, 'market': market, 'state': 'unavailable',
            'error': 'symbol_not_in_snapshot',
            'message': f'{symbol} has not been scored by the scanner yet.',
        }

    # Attach forward metrics on demand for cheap-pass rows.
    fm = row.get('forward_metrics_garch') or row.get('forward_metrics')
    tier_used = 'garch' if row.get('forward_metrics_garch') else 'fast'
    if not fm:
        try:
            from app.services.future_mode_service import attach_forward_metrics_fast
            attach_forward_metrics_fast(row, market=market)
            fm = row.get('forward_metrics')
            tier_used = 'fast'
        except Exception as exc:  # noqa: BLE001
            log.debug('on-demand forward metrics failed for %s: %s', symbol, exc)
    if not fm:
        ctx = build_forecast_context(row)
        return {
            'symbol': symbol, 'market': market, 'state': 'reduced_confidence',
            'reliability': 'reduced',
            'message': 'No factor depth available yet — context-only forecast.',
            'context': ctx,
            'horizons': {},
        }

    if 'forecast_context' not in fm:
        apply_forecast_context(row, fm)
    ctx = fm.get('forecast_context') or build_forecast_context(row)

    horizons = {
        key: _horizon_summary(fm.get(key))
        for key in ('forward_1h', 'forward_1d', 'forward_5d', 'forward_20d', 'forward_overnight')
    }
    horizons = {k: v for k, v in horizons.items() if v}
    summary = summarize_forecast(row) or 'Forecast generated.'

    return {
        'symbol': symbol,
        'market': market,
        'state': 'ok',
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'tier': tier_used,
        'reliability': ctx.get('reliability', 'full'),
        'summary': summary,
        'context': ctx,
        'horizons': horizons,
        'short_selling_pressure_score': row.get('short_selling_pressure_score'),
        'short_selling_pressure_label': row.get('short_selling_pressure_label'),
        'short_selling_pressure_source': row.get('short_selling_pressure_source'),
        'predicted_volume_intensity_score': row.get('predicted_volume_intensity_score'),
        'predicted_volume_intensity_bucket': row.get('predicted_volume_intensity_bucket'),
        'predicted_volume_event_flag': row.get('predicted_volume_event_flag'),
        'nearest_options_expiration': row.get('nearest_options_expiration'),
        'days_to_options_expiration': row.get('days_to_options_expiration'),
        'expiration_risk_flag': row.get('expiration_risk_flag'),
    }


@router.post('/run/{symbol}')
def run_forecast(symbol: str, market: str = Query('stocks'), force: bool = Query(False)):
    sym = (symbol or '').upper()
    now = time.monotonic()
    if not force:
        with _cache_lock:
            cached = _cache.get(sym)
            if cached and (now - cached[0]) < _CACHE_TTL_S:
                payload = dict(cached[1])
                payload['cached'] = True
                return payload
    lk = _lock_for(sym)
    with lk:
        # Re-check under the lock — a concurrent request may have filled it.
        with _cache_lock:
            cached = _cache.get(sym)
            if not force and cached and (time.monotonic() - cached[0]) < _CACHE_TTL_S:
                payload = dict(cached[1])
                payload['cached'] = True
                return payload
        try:
            payload = _build_payload(sym, market or 'stocks')
        except Exception as exc:  # noqa: BLE001
            log.exception('forecast activator failed for %s: %s', sym, exc)
            payload = {'symbol': sym, 'market': market, 'state': 'error', 'error': str(exc)[:200]}
        if payload.get('state') in ('ok', 'reduced_confidence'):
            with _cache_lock:
                _cache[sym] = (time.monotonic(), payload)
                if len(_cache) > 512:
                    oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[:128]
                    for k, _v in oldest:
                        _cache.pop(k, None)
        payload['cached'] = False
        return payload
