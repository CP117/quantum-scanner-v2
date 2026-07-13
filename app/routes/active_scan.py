from fastapi import APIRouter, Query
from app.models.results import StockResultsEnvelope
from app.services.active_scan_service import get_active_scan_results
from app.services.warmer_service import set_warmer_market

router = APIRouter(prefix='/active-scan', tags=['active-scan'])

@router.get('/results', response_model=StockResultsEnvelope)
def active_scan_results(limit: int = Query(250, ge=1, le=500), market: str = Query('stocks')):
    set_warmer_market(market)
    payload = get_active_scan_results(limit=limit, market=market)
    return StockResultsEnvelope(**payload)
