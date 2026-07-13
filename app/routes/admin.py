"""
Admin endpoints for long-run housekeeping.

  GET  /api/admin/maintenance         -> status of the background maintenance loop
  POST /api/admin/rotate-counters     -> manually rotate provider/scan counters
  POST /api/admin/prune-databases     -> manually prune retention-eligible rows
  POST /api/admin/reset-scan-counters -> reset the per-market scan-pass counters

These endpoints are intentionally NOT protected: the dashboard runs locally
(or behind a Cloudflare Quick Tunnel) and the buttons are exposed to the
single operator of the app. If you ever need to lock them down, layer an
HTTP auth header or an env-toggled IP allow-list in front of this router.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

log = logging.getLogger('app.routes.admin')

router = APIRouter(prefix='/api/admin', tags=['admin'])


@router.get('/maintenance')
def maintenance_status():
    """State of the background maintenance loop (last rotation, last prune,
    configured retention windows, DB paths). Read-only.
    """
    from app.services.maintenance_service import maintenance_status as _ms
    return _ms()


@router.post('/rotate-counters')
async def rotate_counters():
    """Force an immediate provider-counter rotation (calls/hits/misses/errors
    reset to 0 across every provider stat dict; last_*_utc and circuit state
    preserved). Returns the per-provider summary of the *previous* counter
    values so the UI can show what was rotated out.
    """
    from app.services.maintenance_service import rotate_provider_counters_async
    summary = await rotate_provider_counters_async()
    return {'ok': True, 'summary': summary}


@router.post('/prune-databases')
async def prune_databases():
    """Force an immediate retention prune across the regulatory and
    saved-predictions SQLite stores. Open predictions are never deleted.
    """
    from app.services.maintenance_service import prune_databases_async
    summary = await prune_databases_async()
    return {'ok': True, 'summary': summary}


@router.post('/reset-scan-counters')
def reset_scan_counters():
    """Hard-reset the per-market scan counters (evaluations_ever,
    current_sweep_scanned, sweeps_completed). Useful when the user wants
    to start a fresh measurement window without restarting the process.

    Bucket contents and provider stats are NOT touched - this only zeroes
    the cumulative scan-loop metadata.
    """
    from app.services import snapshot_store as _ss
    cleared = {}
    for market in ('stocks', 'crypto'):
        lock = _ss._locks.get(market)
        meta = _ss._snapshot_meta.get(market) or {}
        if lock is None:
            continue
        with lock:
            cleared[market] = {
                'evaluations_ever_before': int(meta.get('evaluations_ever') or 0),
                'sweeps_completed_before': int(meta.get('sweeps_completed') or 0),
                'current_sweep_scanned_before': int(meta.get('current_sweep_scanned') or 0),
            }
            meta['evaluations_ever'] = 0
            meta['current_sweep_scanned'] = 0
            meta['sweeps_completed'] = 0
    return {'ok': True, 'cleared': cleared}
