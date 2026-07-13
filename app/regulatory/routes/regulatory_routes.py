"""
Regulatory monitor routes — mounted on the main FastAPI app at /api/regulatory/*.

Mirrors the standalone regulatory_monitor_mvp routes verbatim so the original
launch_*.bat-style usage is preserved when running locally — every endpoint
the standalone UI calls still exists, just under a /api/regulatory prefix.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from app.regulatory.models.schemas import MonitorResponse
from app.regulatory.services.monitor_service import (
    run_scan,
    scheduled_poll_once,
    scan_tracked_companies,
    auto_scan_universe,
    autoscan_snapshot,
    request_autoscan_cancel,
)
from app.regulatory.services.discovery_service import discover_new_insider_companies
from app.regulatory.services.storage_service import (
    save_watchlist,
    list_watchlists,
    list_alerts,
    get_settings,
    set_setting,
    get_stats,
    list_tracked_companies,
    get_tracked_company_detail,
    get_alert_detail,
    get_award_detail,
    get_filing_detail,
)
from app.regulatory.services.signal_service import (
    get_signal_for_symbol,
    get_signal_summary,
    refresh_signal_index,
)


router = APIRouter(prefix='/api/regulatory', tags=['regulatory'])


class WatchlistIn(BaseModel):
    cik: str
    recipient: str


class SettingIn(BaseModel):
    key: str
    value: str


@router.get('/health')
async def regulatory_health():
    return {'ok': True, 'service': 'regulatory_monitor'}


@router.get('/monitor', response_model=MonitorResponse)
async def monitor(cik: str = Query(...), recipient: str = Query(...), limit: int = 10):
    result = await run_scan(cik, recipient, limit=limit)
    return MonitorResponse(**result)


@router.post('/watchlists')
async def add_watchlist(item: WatchlistIn):
    await save_watchlist(item.cik, item.recipient)
    return {'ok': True}


@router.get('/watchlists')
async def get_watchlists_api():
    return await list_watchlists()


@router.get('/tracked-companies')
async def get_tracked_companies_api(limit: int = 200):
    return await list_tracked_companies(limit=limit)


@router.get('/tracked-companies/{cik}')
async def get_tracked_company_api(cik: str):
    detail = await get_tracked_company_detail(cik)
    if not detail:
        raise HTTPException(status_code=404, detail='Tracked company not found')
    return detail


@router.post('/discover')
async def discover_api(limit: int = 200):
    count = await discover_new_insider_companies(limit=limit)
    return {'ok': True, 'discovered': count}


@router.post('/scan-tracked')
async def scan_tracked_api(limit_per_company: int = 5):
    processed = await scan_tracked_companies(limit_per_company=limit_per_company)
    return {'ok': True, 'processed': processed}


@router.get('/filings/{unique_key}')
async def get_filing_api(unique_key: str):
    detail = await get_filing_detail(unique_key)
    if not detail:
        raise HTTPException(status_code=404, detail='Filing not found')
    return detail


@router.get('/awards/{generated_internal_id}')
async def get_award_api(generated_internal_id: str):
    detail = await get_award_detail(generated_internal_id)
    if not detail:
        raise HTTPException(status_code=404, detail='Award not found')
    return detail


@router.get('/alerts')
async def get_alerts_api(limit: int = 100):
    return await list_alerts(limit)


@router.get('/alerts/{alert_id}')
async def get_alert_api(alert_id: int):
    detail = await get_alert_detail(alert_id)
    if not detail:
        raise HTTPException(status_code=404, detail='Alert not found')
    return detail


@router.post('/poll')
async def poll_watchlists(limit: int = 0):
    results = await scheduled_poll_once(limit=limit)
    return {'ok': True, 'watchlists_processed': len(results)}


@router.get('/settings')
async def get_settings_api():
    return await get_settings()


@router.post('/settings')
async def set_settings_api(item: SettingIn):
    await set_setting(item.key, item.value)
    return {'ok': True}


@router.get('/stats')
async def get_stats_api():
    return await get_stats()


# ---------------------------------------------------------------------------
# Scoring-bridge endpoints — let the main scanner inspect what regulatory
# signal (if any) is applied to a given symbol's composite score.
# ---------------------------------------------------------------------------

@router.get('/signal/{symbol}')
async def regulatory_signal_for_symbol(symbol: str):
    """Return the current regulatory signal applied to a given ticker.

    Output: {score_delta, weight, reason, staleness_days, raw_events}
    """
    return await get_signal_for_symbol(symbol)


@router.get('/signal-summary')
async def regulatory_signal_summary(limit: int = 100):
    """Top-N tickers currently carrying a non-zero regulatory signal."""
    return await get_signal_summary(limit=limit)


@router.post('/signal-refresh')
async def regulatory_signal_refresh():
    """Force the in-process signal index to rebuild from SQLite right now.

    The signal cache auto-refreshes on a TTL — this endpoint just gives the
    UI a button to force an immediate rescan after a manual poll.
    """
    count = await refresh_signal_index()
    return {'ok': True, 'symbols_indexed': count}


# ---------------------------------------------------------------------------
# Universe auto-scan endpoints — drives the auto-populating result list.
# ---------------------------------------------------------------------------

@router.get('/autoscan-status')
async def autoscan_status_api():
    """Current state of the universe auto-scan loop:
    {enabled, tickers_total, tickers_done, tickers_with_hits, current_symbol,
     started_at, last_completed_at, last_sweep_seconds, cik_map_size, busy}
    """
    return autoscan_snapshot()


@router.post('/autoscan-trigger')
async def autoscan_trigger_api():
    """Kick off a universe auto-scan immediately (in the background).

    Returns immediately so the user's HTTP request doesn't hang for the
    multi-minute sweep. Subscribe to /autoscan-status to see progress.
    """
    import asyncio
    # Already busy? Don't spawn a duplicate; just report the in-progress
    # state so the UI keeps polling for the existing sweep.
    snap = autoscan_snapshot()
    if snap.get('busy'):
        return {
            'ok': True,
            'started': False,
            'message': 'sweep already in progress',
            'state': snap,
        }
    asyncio.create_task(auto_scan_universe())
    return {
        'ok': True,
        'started': True,
        'message': 'autoscan kicked off; poll /api/regulatory/autoscan-status',
    }


@router.post('/autoscan-cancel')
async def autoscan_cancel_api():
    """Cooperative cancel — signals the in-flight sweep to stop at the next
    ticker boundary. Returns immediately."""
    request_autoscan_cancel()
    return {'ok': True, 'message': 'cancel requested; sweep will stop on the next ticker'}


@router.get('/auto-results')
async def auto_results_api(limit: int = 200):
    """Auto-populated sorted result list — every ticker with recent insider
    activity OR a recent contract award. Powers the new subpage view.
    """
    from app.regulatory.services.signal_service import get_auto_results_list
    return await get_auto_results_list(limit=limit)
