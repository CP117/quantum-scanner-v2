"""Admin/debug surface for the cache deduplication subsystem."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query

from app.services.cache_dedupe_service import dedupe_status, run_full_dedupe

router = APIRouter(prefix='/api/cache/dedupe', tags=['cache'])


@router.get('/status')
def get_dedupe_status():
    return dedupe_status()


@router.post('/run')
async def trigger_dedupe(trigger: str = Query('manual_admin')):
    result = await asyncio.to_thread(run_full_dedupe, trigger)
    return {'ok': True, 'result': result, 'status': dedupe_status()}


@router.get('/memory/status')
def get_memory_cache_status():
    """Aggregated process-local cache diagnostics; no cache keys or payloads."""
    from app.services.memory_store import memory_store
    return memory_store.get_stats()


@router.post('/memory/invalidate')
def invalidate_memory_cache(
    domain: str = Query('all'),
    symbol: str | None = Query(None),
):
    """Explicitly invalidate RAM entries without exposing cache contents."""
    from app.services.memory_store import memory_store

    domains = {item.strip() for item in domain.split(',') if item.strip()}
    if not domains or domains == {'all'}:
        domains = {'options_chains', 'universe_metadata', 'scanner_presets', 'narratives', 'bayesian_priors'}
    supported = {'options_chains', 'universe_metadata', 'scanner_presets', 'narratives', 'bayesian_priors'}
    unknown = domains - supported
    if unknown:
        raise HTTPException(status_code=400, detail=f'unsupported_cache_domains:{",".join(sorted(unknown))}')

    removed = 0
    for item in domains:
        if item == 'scanner_presets':
            from app.services.scanner_presets import clear_compiled_presets
            clear_compiled_presets()
            continue
        if item == 'universe_metadata':
            removed += memory_store.invalidate_domain(item)
            from app.services.universe_service import bust_crypto_universe_cache, bust_universe_cache
            bust_universe_cache()
            bust_crypto_universe_cache()
            continue
        if item == 'narratives':
            removed += memory_store.invalidate_domain(item)
            continue
        removed += (
            memory_store.invalidate_symbol(symbol, domains={item})
            if symbol else memory_store.invalidate_domain(item)
        )
        if item == 'options_chains':
            from app.services.options_chain_service import invalidate_cached_chains
            invalidate_cached_chains(symbol)
    return {'ok': True, 'domains': sorted(domains), 'symbol': symbol, 'removed': removed}
