"""Read-only catalog browse including latest stored market state (Phase 3).

GET only; reads the durable catalog DB and never contacts providers, runs
ingestion or triggers analysis. Instruments without a quote stay in the
response with ``market_state: null``. Public display rights are enforced here:
a stored quote is returned only if the configured entitlement registry allows
public display for its provider+kind; otherwise it is withheld with a reason.
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.ingestion import market_state_store as store
from app.ingestion.contracts import DataKind
from app.ingestion.rights import EntitlementRegistry, registry_from_config
from app.routes.catalog import _filters, get_catalog_conn
from app.services import catalog_service as cs

router = APIRouter(prefix='/api/market', tags=['market-state'])


def get_entitlements() -> EntitlementRegistry:
    from app.config import settings
    return registry_from_config(settings.market_data_display_entitlements)


def get_now() -> float:
    return time.time()


@router.get('/catalog')
def catalog_with_market_state(filters: dict = Depends(_filters),
                              after: Optional[str] = Query(None, max_length=512),
                              limit: int = Query(50, ge=1, le=200),
                              max_age_seconds: float = Query(900.0, gt=0, le=604800),
                              conn=Depends(get_catalog_conn),
                              rights: EntitlementRegistry = Depends(get_entitlements),
                              now: float = Depends(get_now)):
    try:
        page = cs.list_instruments(conn, after=after, limit=limit, **filters)
    except cs.CatalogQueryError as exc:
        raise HTTPException(400, str(exc)) from exc
    states = store.latest_for(conn, [i['instrument_id'] for i in page['items']],
                              now_ts=now, max_age_s=max_age_seconds)
    items = []
    for inst in page['items']:
        quotes = states.get(inst['instrument_id'], [])
        shown = [q for q in quotes if rights.may_display_publicly(q['provider'], DataKind(q['kind']))]
        item = dict(inst)
        item['market_state'] = shown or None
        if quotes and not shown:
            item['market_state_withheld'] = 'display_rights_not_verified'
        items.append(item)
    return {'items': items, 'next_cursor': page['next_cursor']}
