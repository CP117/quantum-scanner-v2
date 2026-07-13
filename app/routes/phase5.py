"""
Manual refresh, user-symbol management, NASDAQ refresh, and backtest routes.

All Phase 5 endpoints live here so they're easy to find and audit.
Routes are unprefixed (no /api) to stay consistent with the rest of the app.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Body

from app.models.results import StockResultRow
from app.services.backtest_service import run_backtest
from app.services.detail_service import get_symbol_detail
from app.services.quote_cache import invalidate_quote
from app.services.universe_extras import (
    add_user_symbol,
    get_user_added,
    refresh_nasdaq_listing,
    remove_user_symbol,
)
from app.services.daily_history_service import invalidate as invalidate_daily_history

log = logging.getLogger('app.phase5_routes')

router = APIRouter(tags=['phase5'])


# ---------------------------------------------------------------------------
# Manual quote refresh
# ---------------------------------------------------------------------------

@router.post('/stock/{symbol}/refresh', response_model=StockResultRow)
def refresh_stock(symbol: str, market: str = Query('stocks')):
    """Force a fresh live fetch for one symbol, bypassing in-memory caches.

    Phase 22 fix:
      - Previously we invalidated the quote cache BEFORE the live fetch.
        If the live fetch then failed (rate-limit storm, provider outage,
        crypto pre-Phase-22 lacking a cascade), we returned an empty
        unavailable stub with last_price=0 — and worse, the freshly-empty
        cache meant subsequent reads would ALSO return empty.  The UI
        showed `Refresh stale data` forever with nothing to refresh from.
      - Now we keep the cache intact during the live fetch.  If live
        succeeds, the new save_quote() call inside score_from_prices
        transparently overwrites the stale row.  If live fails we
        fall back to the still-present cached row (with a clear
        `refresh_failed=true` flag so the UI can tell the difference).

    Also invalidates the daily-history cache so reaction clustering,
    volume sentiment, and the IOB heuristic get recomputed against fresh
    bars on the next batch scan that visits this symbol.
    """
    # Phase 25: any manual refresh of a crypto symbol is a strong signal
    # that the user is engaged with the crypto market — open the live
    # provider cascade for crypto.
    from app.services.market_activity_service import stamp_active
    stamp_active(market)
    sym = (symbol or '').strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail='empty_symbol')

    # Daily-history is safe to invalidate up-front — its async prefetch
    # pool will repopulate within ~3s and the detail call will block on it
    # via _safe_daily.
    invalidate_daily_history(sym)

    payload = get_symbol_detail(sym, force_live=True, market=market)
    if not payload:
        raise HTTPException(status_code=404, detail=f'no_data_for_{sym}')

    # Detect failed-live scenarios so the frontend can show an honest
    # "live providers unavailable, showing last-known-good" message
    # instead of pretending the refresh succeeded.
    #   live_success            -> refresh worked, data is fresh
    #   cache_after_live_failed -> live attempted, all providers failed;
    #                              falling back to last-known-good cache
    #   live_failed/unavailable -> nothing to show at all
    outcome = payload.get('provider_outcome') or ''
    if outcome in ('live_failed', 'unavailable', 'cache_after_live_failed'):
        payload['refresh_failed'] = True
    else:
        payload['refresh_failed'] = False

    # Phase 12: manual refresh on the host PC should ALSO update the
    # broadcast snapshot so every connected client (phone, other laptop)
    # sees the freshly-fetched row on its next poll.
    try:
        from app.services.snapshot_store import upsert_rows
        upsert_rows(market, [payload])
    except Exception:
        pass
    return payload


# ---------------------------------------------------------------------------
# User-added symbols
# ---------------------------------------------------------------------------

@router.get('/universe/added')
def list_added() -> dict:
    return {'symbols': get_user_added()}


@router.post('/universe/add')
def add_symbol(payload: dict = Body(...)):
    sym = (payload.get('symbol') or '').strip().upper()
    name = payload.get('name')
    exchange = payload.get('exchange') or 'USER_ADDED'
    result = add_user_symbol(sym, name=name, exchange=exchange)
    if not result.get('ok'):
        raise HTTPException(status_code=400, detail=result.get('reason', 'invalid'))
    return result


@router.delete('/universe/remove/{symbol}')
def remove_symbol(symbol: str):
    result = remove_user_symbol(symbol)
    if not result.get('ok'):
        raise HTTPException(status_code=404, detail=result.get('reason', 'not_found'))
    return result


@router.post('/universe/refresh-nasdaq')
def refresh_nasdaq(force: bool = Query(False)):
    summary = refresh_nasdaq_listing(force=force)
    try:
        from app.services.universe_service import bust_universe_cache
        bust_universe_cache()
    except Exception:
        pass
    return summary


# ---------------------------------------------------------------------------
# Backtest harness
# ---------------------------------------------------------------------------

@router.get('/backtest/{symbol}')
def backtest(
    symbol: str,
    lookback: int = Query(180, ge=30, le=500),
    warmup: int = Query(30, ge=10, le=200),
    forward_bars: int = Query(5, ge=1, le=30),
):
    return run_backtest(
        symbol=symbol,
        lookback_bars=lookback,
        warmup_bars=warmup,
        forward_bars=forward_bars,
    )
