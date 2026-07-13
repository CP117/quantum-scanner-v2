
from fastapi import APIRouter, Query
from app.config import settings
from app.models.search import SearchResultsEnvelope, SearchResultRow
from app.services.universe_service import search_universe
from app.utils.input_tolerance import normalize_search_query

router = APIRouter()


def _run_search(q: str, market: str = 'both') -> SearchResultsEnvelope:
    # Phase 26.18.c: normalize the user query before hitting the universe
    # so trailing whitespace / mixed case / repeated spaces don't cause
    # zero-result misses. Empty result after normalization returns an
    # empty envelope (not a 400) so the UI can show "no matches".
    cleaned = normalize_search_query(q)
    if not cleaned:
        return SearchResultsEnvelope(query=q or '', total=0, results=[])
    rows = [
        SearchResultRow(
            symbol=row.get('symbol', ''),
            name=row.get('name', ''),
            exchange=row.get('exchange', ''),
        )
        for row in search_universe(cleaned, settings.max_search_results, market=market)
    ]
    return SearchResultsEnvelope(query=cleaned, total=len(rows), results=rows)


@router.get('/search', response_model=SearchResultsEnvelope)
def search(q: str = Query('', min_length=0),
           market: str = Query('both', pattern='^(stocks|crypto|both)$')):
    return _run_search(q, market=market)


@router.get('/search/symbols', response_model=SearchResultsEnvelope)
def search_symbols(q: str = Query('', min_length=0),
                   market: str = Query('both', pattern='^(stocks|crypto|both)$')):
    return _run_search(q, market=market)
