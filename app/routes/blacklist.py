"""
Blacklist administration routes — list / unblock persistently-failing
symbols so the user can audit which tickers the scanner has parked.
"""
from __future__ import annotations

import asyncio
from fastapi import APIRouter, HTTPException, Query

from app.services import symbol_blacklist_service as bl

router = APIRouter(prefix='/api/blacklist', tags=['blacklist'])


@router.get('/list')
async def blacklist_list(limit: int = Query(500, ge=1, le=5000)):
    """Return the currently blacklisted symbols sorted by most-recently
    promoted first.  Each row includes failure counts and the list of
    UTC days the symbol failed on so the user can sanity-check."""
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, bl.list_blacklisted)
    rows = rows[: int(limit or 500)]
    stats = await loop.run_in_executor(None, bl.stats)
    return {'rows': rows, 'total': len(rows), 'stats': stats}


@router.post('/unblock/{symbol}')
async def blacklist_unblock(symbol: str):
    """Remove a symbol from the blacklist + reset its failure history.

    Useful when a provider was flapping during a multi-day outage and the
    user wants to give a symbol a clean slate."""
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(None, bl.unblock, symbol)
    if not ok:
        raise HTTPException(status_code=404, detail='symbol_not_blacklisted')
    return {'ok': True, 'symbol': symbol.upper()}


@router.get('/stats')
async def blacklist_stats():
    """Lightweight summary surfaced on the main /system/status panel."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, bl.stats)
