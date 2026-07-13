"""Cross-Market Squeeze Radar route (Phase 26.70).

Exposes the radar payload assembled by `cross_market_radar_service`.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.services.cross_market_radar_service import compute_cross_market_radar

router = APIRouter(tags=['cross-market-radar'])


@router.get('/api/scan/cross-market-squeeze')
def cross_market_squeeze(
    limit_per_market: int = Query(20, ge=1, le=100),
    universe_scan_limit: int = Query(100, ge=20, le=500),
):
    return compute_cross_market_radar(
        limit_per_market=limit_per_market,
        universe_scan_limit=universe_scan_limit,
    )
