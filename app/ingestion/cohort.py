"""Small verified stock cohort for the first ingestion slice (provider: yahoo-chart).

Venue aliases are the provider's ``exchangeName`` codes mapped to MIC venue IDs
(NMS=Nasdaq Global Select -> XNAS, NYQ=NYSE -> XNYS, PCX=NYSE Arca -> ARCX).
Only these reviewed aliases are loaded; anything else is rejected at ingestion.
KO is in the catalog deliberately without a quote fixture.
"""
from __future__ import annotations

from app.services import catalog_service as cs

PROVIDER = 'yahoo-chart'
UNIVERSE_KEY = 'phase3_stock_cohort'
VENUE_ALIASES = {'NMS': 'XNAS', 'NYQ': 'XNYS', 'PCX': 'ARCX'}
COHORT = [
    {'asset_class': 'equity', 'venue': 'XNAS', 'symbol': 'AAPL', 'name': 'Apple Inc.', 'provider_symbol': 'AAPL'},
    {'asset_class': 'equity', 'venue': 'XNAS', 'symbol': 'MSFT', 'name': 'Microsoft Corp.', 'provider_symbol': 'MSFT'},
    {'asset_class': 'equity', 'venue': 'XNYS', 'symbol': 'JPM', 'name': 'JPMorgan Chase & Co.', 'provider_symbol': 'JPM'},
    {'asset_class': 'equity', 'venue': 'ARCX', 'symbol': 'SPY', 'name': 'SPDR S&P 500 ETF Trust',
     'instrument_type': 'etf', 'provider_symbol': 'SPY'},
    {'asset_class': 'equity', 'venue': 'XNYS', 'symbol': 'KO', 'name': 'Coca-Cola Co.', 'provider_symbol': 'KO'},
]


def bootstrap(conn) -> dict:
    """Idempotently load aliases, catalog rows and provider mappings."""
    for alias, venue in VENUE_ALIASES.items():
        cs.add_venue_alias(conn, PROVIDER, alias, venue)
    return cs.sync_listing(conn, PROVIDER, UNIVERSE_KEY, COHORT, complete=False)
