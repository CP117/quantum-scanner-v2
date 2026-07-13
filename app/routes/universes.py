"""Phase 26.66 — Toggleable universe groups.

Endpoints:
  * GET  /api/universes?market=stocks|crypto — list every group with its
        symbol count + active flag.
  * POST /api/universes/toggle               — activate/deactivate one group.
        Body: { "market": "stocks", "key": "nasdaq_1_10", "active": true }
        Clears that market's snapshot so the next sweep reflects the new
        active universe (removed groups' symbols disappear; newly-activated
        groups get scanned fresh).
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Query

from app.services import universe_service as us

router = APIRouter(prefix='/api/universes', tags=['universes'])


@router.get('')
def list_groups(market: str = Query('stocks', regex='^(stocks|crypto)$')):
    groups = us.list_universe_groups(market)
    return {
        'market': market,
        'groups': groups,
        'active_count': sum(1 for g in groups if g['active']),
        'active_symbol_count': sum(g['count'] for g in groups if g['active']),
    }


@router.get('/integrity')
def universe_integrity():
    """Health snapshot of the persisted active-universe state.  Consumed
    by the Metrics Hub and lets ops verify the schema + baseline are
    intact after any code deploy / manual edit."""
    return us.universe_integrity_status()


@router.post('/toggle')
def toggle_group(payload: dict = Body(...)):
    market = 'crypto' if payload.get('market') == 'crypto' else 'stocks'
    key = str(payload.get('key') or '')
    active = bool(payload.get('active'))
    result = us.set_group_active(market, key, active)
    if result.get('ok'):
        # Bust caches and clear the market snapshot so the next sweep
        # reflects the new active universe immediately.
        try:
            us.bust_universe_cache()
            us.bust_crypto_universe_cache()
            from app.services.snapshot_store import clear_snapshot
            clear_snapshot(market)
        except Exception:  # noqa: BLE001
            pass
    return result
