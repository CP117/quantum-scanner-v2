"""Read-only catalog browse endpoints (Phase 2, pending validation).

Served purely from the durable catalog.  These handlers never call providers,
universe loaders, sync or analysis code; seeding/sync is an explicit
operator command (``python -m app.services.catalog_seed``).  The connection
is a FastAPI dependency so tests can inject a temporary database.
"""
from __future__ import annotations

from typing import Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.services import catalog_service as cs

router = APIRouter(prefix='/api/catalog', tags=['catalog'])


def get_catalog_conn() -> Iterator:
    conn = cs.connect()
    try:
        yield conn
    finally:
        conn.close()


def _filters(q: Optional[str] = Query(None, max_length=100),
             asset_class: Optional[str] = Query(None, max_length=32),
             instrument_type: Optional[str] = Query(None, max_length=32),
             venue: Optional[str] = Query(None, max_length=64),
             status: Optional[str] = Query(None, max_length=32),
             universe: Optional[str] = Query(None, max_length=128)) -> dict:
    if asset_class and asset_class not in cs.ASSET_CLASSES:
        raise HTTPException(400, 'invalid asset_class')
    if status and status not in cs.STATUSES:
        raise HTTPException(400, 'invalid status')
    return {'q': q, 'asset_class': asset_class, 'instrument_type': instrument_type,
            'venue': venue, 'status': status, 'universe_key': universe}


@router.get('/instruments')
def instruments(filters: dict = Depends(_filters),
                after: Optional[str] = Query(None, max_length=512),
                limit: int = Query(50, ge=1, le=200),
                conn=Depends(get_catalog_conn)):
    try:
        return cs.list_instruments(conn, after=after, limit=limit, **filters)
    except cs.CatalogQueryError as exc:
        raise HTTPException(400, str(exc))


@router.get('/counts')
def counts(filters: dict = Depends(_filters), conn=Depends(get_catalog_conn)):
    return cs.catalog_counts(conn, **filters)
