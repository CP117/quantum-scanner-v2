from fastapi import APIRouter, Query, HTTPException
from app.services.detail_service import get_symbol_detail
from app.utils.input_tolerance import loose_bool, normalize_market, normalize_symbol

router = APIRouter()


@router.get('/stock/{symbol}')
def stock_detail(
    symbol: str,
    force_live: str | bool = Query(False),
    require_fresh: str | bool = Query(False),
    market: str = Query('stocks'),
):
    """Per-symbol detail endpoint.

    `require_fresh=True` (Phase 26.68) always triggers a re-fetch of the
    live quote + rescore against the cached daily-hist + intraday shape.
    This is the flag the detail panel's 2-second live tick sends so
    filter-elevated symbols don't stay stuck at partial batch-cheap-pass
    metrics.  Because it reuses the batched quote cache (short TTL) it
    doesn't hammer providers.

    `force_live=True` is heavier — full intraday + daily re-download.
    Reserved for the "Refresh now" button (unblock a lockup / clear a
    rate-limit stall).
    """
    sym = normalize_symbol(symbol)
    if not sym:
        raise HTTPException(status_code=400, detail='symbol must not be empty')
    mkt = normalize_market(market, default='stocks')
    fl = loose_bool(force_live, default=False)
    rf = loose_bool(require_fresh, default=False)
    return get_symbol_detail(sym, force_live=fl, market=mkt, require_fresh=rf)
