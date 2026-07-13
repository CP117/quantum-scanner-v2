"""Admin/debug surface for the cache deduplication subsystem."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query

from app.services.cache_dedupe_service import dedupe_status, run_full_dedupe

router = APIRouter(prefix='/api/cache/dedupe', tags=['cache'])


@router.get('/status')
def get_dedupe_status():
    return dedupe_status()


@router.post('/run')
async def trigger_dedupe(trigger: str = Query('manual_admin')):
    result = await asyncio.to_thread(run_full_dedupe, trigger)
    return {'ok': True, 'result': result, 'status': dedupe_status()}
