"""Read-only catalog browse endpoints (Phase 2).

Never triggers analysis or provider calls; served purely from the durable
catalog so instruments are visible before any quote/score exists.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.services import catalog_service as cs

router = APIRouter(prefix='/api/catalog', tags=['catalog'])


@router.get('/instruments')
def instruments(q: Optional[str] = None, asset_type: Optional[str] = None,
                venue: Optional[str] = None, status: Optional[str] = None,
                universe: Optional[str] = None, after: Optional[str] = None,
                limit: int = Query(50, ge=1, le=200)):
    if asset_type and asset_type not in cs.ASSET_TYPES:
        raise HTTPException(400, 'invalid asset_type')
    if status and status not in cs.STATUSES:
        raise HTTPException(400, 'invalid status')
    conn = cs.connect()
    try:
        return cs.list_instruments(conn, q=q, asset_type=asset_type, venue=venue, status=status,
                                   universe_key=universe, after=after, limit=limit)
    finally:
        conn.close()


@router.get('/counts')
def counts():
    conn = cs.connect()
    try:
        return cs.catalog_counts(conn)
    finally:
        conn.close()
