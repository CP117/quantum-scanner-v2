"""
Tier Status & Reset API Routes — Phase 28
==========================================

Endpoints
---------
    GET  /api/tier-status        — per-tier health (active count, queue depth,
                                   last rescan, watchdog age, errors, stall prompt)
    POST /api/tier/{tier}/reset  — user-initiated tier reset (clears locks,
                                   caches, restarts tier)
    GET  /system/tier-status     — alias for /api/tier-status (internal)
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Path

router = APIRouter(tags=['tier_status'])


@router.get('/api/tier-status')
@router.get('/system/tier-status')
def tier_status():
    """Return per-tier health + stall prompts for the dashboard."""
    result: dict = {
        'timestamp': time.time(),
        'tiers': {},
        'stall_prompts': [],
        'tier_manager': {},
        'gpu': {},
    }

    # Per-tier health from resilience module.
    try:
        from app.services.tier_resilience import get_tier_health, get_all_stall_prompts
        result['tiers'] = get_tier_health()
        result['stall_prompts'] = get_all_stall_prompts()
    except Exception as exc:  # noqa: BLE001
        result['tiers_error'] = str(exc)

    # Tier manager overview (symbol counts per tier).
    try:
        from app.services.tier_manager import get_status as tm_status
        result['tier_manager'] = tm_status()
    except Exception as exc:  # noqa: BLE001
        result['tier_manager_error'] = str(exc)

    # GPU info.
    try:
        from app.services.gpu_acceleration import GPU_AVAILABLE
        result['gpu'] = {'available': GPU_AVAILABLE}
    except Exception as exc:  # noqa: BLE001
        result['gpu'] = {'available': False, 'error': str(exc)}

    # Scanner-level telemetry.
    try:
        from app.services.tier_1_active_scanner import get_status as t1_status
        result['tier_1_scanner'] = t1_status()
    except Exception as exc:  # noqa: BLE001
        result['tier_1_scanner'] = {'error': str(exc)}

    try:
        from app.services.tier_2_monitor_scanner import get_status as t2_status
        result['tier_2_scanner'] = t2_status()
    except Exception as exc:  # noqa: BLE001
        result['tier_2_scanner'] = {'error': str(exc)}

    try:
        from app.services.tier_3_background_scanner import get_status as t3_status
        result['tier_3_scanner'] = t3_status()
    except Exception as exc:  # noqa: BLE001
        result['tier_3_scanner'] = {'error': str(exc)}

    # Tier cache stats.
    try:
        from app.services.tier_cache_store import cache_status
        result['tier_cache'] = cache_status()
    except Exception as exc:  # noqa: BLE001
        result['tier_cache'] = {'error': str(exc)}

    return result


@router.post('/api/tier/{tier}/reset')
def reset_tier(tier: int = Path(..., ge=1, le=3)):
    """User-initiated tier reset.

    Clears the tier's stall flag, resets its error counters, and optionally
    flushes the tier-specific cache.  The scanner threads are self-healing
    daemon loops — they automatically resume after a reset.
    """
    try:
        from app.services.tier_resilience import reset_tier as do_reset
        return do_reset(tier, clear_caches=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
