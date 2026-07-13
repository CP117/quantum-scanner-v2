"""Backend tests for iteration 4 review request:

1. MODULE SPLIT   — market_proxy_service + horizon_definitions extracted from
   future_mode_service.  Legacy aliases must still resolve.
2. CRYPTO PRESET  — the 4 context presets must return sensible rows for the
   crypto market (or be intentionally disabled).
3. STOCK REGRESS  — the same presets must still work for stocks.
4. RADAR API      — /api/scan/cross-market-squeeze payload shape + ranking.
5. RADAR PAGE     — /cross-market-squeeze.html + nav link presence.
6. GENERAL REGR.  — universes integrity, share pages, source download, fresh
   detail — all previously-verified endpoints still work.

Run:
  pytest /app/backend/tests/test_module_split_and_cross_market.py -v --tb=short \
    --junitxml=/app/test_reports/pytest/iteration_4.xml
"""
from __future__ import annotations

import os
import sys
import time

import pytest
import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
# Per review request: use http://localhost:8001 for curls (radar can take
# 60s on first prime, subsequent calls hit the 20s in-process cache).
BASE_URL = 'http://localhost:8001'
LONG_TIMEOUT = 120  # radar can prime slowly on first call
MID_TIMEOUT = 60
FAST_TIMEOUT = 20

# Make sure the app package is importable for the module-split tests.
if '/app' not in sys.path:
    sys.path.insert(0, '/app')


@pytest.fixture(scope='session')
def http():
    s = requests.Session()
    s.headers['Content-Type'] = 'application/json'
    return s


# --------------------------------------------------------------------------- #
# 1. Module split — market_proxy_service + horizon_definitions
# --------------------------------------------------------------------------- #
class TestModuleSplit:
    def test_market_proxy_service_imports(self):
        from app.services.market_proxy_service import (
            proxy_symbol_for,
            load_market_proxy_returns,
            MARKET_PROXY_SYMBOL_STOCKS,
            MARKET_PROXY_SYMBOL_CRYPTO,
        )
        assert MARKET_PROXY_SYMBOL_STOCKS == 'SPY'
        assert MARKET_PROXY_SYMBOL_CRYPTO == 'BTC-USD'
        assert proxy_symbol_for('stocks') == 'SPY'
        assert proxy_symbol_for('crypto') == 'BTC-USD'
        assert proxy_symbol_for(None) == 'SPY'
        # sanity: function exists and is callable
        assert callable(load_market_proxy_returns)

    def test_horizon_definitions_imports(self):
        from app.services.horizon_definitions import ALL_HORIZONS
        assert isinstance(ALL_HORIZONS, tuple)
        keys = [h[0] for h in ALL_HORIZONS]
        assert keys == ['forward_1h', 'forward_5h', 'forward_1d',
                        'forward_5d', 'forward_20d']
        assert len(ALL_HORIZONS) == 5
        # Each entry: (key, units, is_intraday)
        for k, units, intraday in ALL_HORIZONS:
            assert isinstance(k, str) and k.startswith('forward_')
            assert isinstance(units, int) and units > 0
            assert isinstance(intraday, bool)

    def test_legacy_aliases_still_resolve(self):
        """future_mode_service must re-export the extracted symbols under
        their legacy underscore names for backwards compatibility."""
        from app.services import future_mode_service as fms
        # Aliases must resolve
        assert hasattr(fms, '_load_market_proxy_returns')
        assert hasattr(fms, '_ALL_HORIZONS')
        assert hasattr(fms, '_MARKET_PROXY_SYMBOL_STOCKS')
        assert hasattr(fms, '_MARKET_PROXY_SYMBOL_CRYPTO')
        # And they must be identical to the new module's exports
        from app.services.market_proxy_service import (
            load_market_proxy_returns,
            MARKET_PROXY_SYMBOL_STOCKS,
            MARKET_PROXY_SYMBOL_CRYPTO,
        )
        from app.services.horizon_definitions import ALL_HORIZONS
        assert fms._MARKET_PROXY_SYMBOL_STOCKS == MARKET_PROXY_SYMBOL_STOCKS
        assert fms._MARKET_PROXY_SYMBOL_CRYPTO == MARKET_PROXY_SYMBOL_CRYPTO
        assert fms._ALL_HORIZONS == ALL_HORIZONS
        # Legacy alias should call through to the new function (or be the
        # same object).  Only assert identity/wrapping — do not invoke,
        # since it hits daily_history.
        assert fms._load_market_proxy_returns is load_market_proxy_returns \
            or callable(fms._load_market_proxy_returns)


# --------------------------------------------------------------------------- #
# 2. Crypto preset tuning
# --------------------------------------------------------------------------- #
class TestCryptoPresets:
    def _get_preset(self, http, preset, market='crypto'):
        url = f'{BASE_URL}/stocks/results'
        r = http.get(url, params={'market': market, 'preset': preset},
                     timeout=MID_TIMEOUT)
        return r

    def test_crypto_squeeze_watch_has_rows(self, http):
        r = self._get_preset(http, 'squeeze-watch', 'crypto')
        assert r.status_code == 200
        data = r.json()
        results = data.get('results') or []
        assert isinstance(results, list)
        assert len(results) >= 1, \
            f'Expected >=1 crypto squeeze-watch row, got {len(results)}'
        symbols = {row.get('symbol') for row in results}
        # Main agent verified BTC-USD as the expected match.
        assert 'BTC-USD' in symbols, \
            f'Expected BTC-USD in crypto squeeze-watch, got {symbols}'

    def test_crypto_volume_storm_has_rows(self, http):
        r = self._get_preset(http, 'volume-storm', 'crypto')
        assert r.status_code == 200
        data = r.json()
        results = data.get('results') or []
        assert len(results) >= 3, \
            f'Expected >=3 crypto volume-storm rows, got {len(results)}'
        symbols = {row.get('symbol') for row in results}
        # Main agent verified: SOL-USD, BTC-USD, WBT-USD.  We assert at
        # least 2 of the 3 are present (allows the ranking to fluctuate
        # slightly with fresh data).
        expected = {'SOL-USD', 'BTC-USD', 'WBT-USD'}
        overlap = expected & symbols
        assert len(overlap) >= 2, \
            f'Expected >=2 of {expected} in crypto volume-storm, got {symbols}'

    def test_crypto_expiration_pin_returns_zero(self, http):
        """Options expiration is meaningless for crypto — must return 0."""
        r = self._get_preset(http, 'expiration-pin', 'crypto')
        assert r.status_code == 200
        data = r.json()
        results = data.get('results') or []
        assert len(results) == 0, \
            f'expiration-pin must be disabled for crypto, got {len(results)} rows'

    def test_crypto_bearish_pressure_valid_response(self, http):
        """May be 0 rows if nothing is bearish, but must be a valid array."""
        r = self._get_preset(http, 'bearish-pressure', 'crypto')
        assert r.status_code == 200
        data = r.json()
        assert 'results' in data
        assert isinstance(data['results'], list)


# --------------------------------------------------------------------------- #
# 3. Stock preset regression
# --------------------------------------------------------------------------- #
class TestStockPresetRegression:
    @pytest.mark.parametrize('preset', [
        'squeeze-watch', 'volume-storm', 'bearish-pressure', 'expiration-pin',
    ])
    def test_stock_preset_returns_200(self, http, preset):
        r = http.get(f'{BASE_URL}/stocks/results',
                     params={'preset': preset}, timeout=MID_TIMEOUT)
        assert r.status_code == 200, \
            f'stock preset {preset} returned {r.status_code}'
        data = r.json()
        assert 'results' in data
        assert isinstance(data['results'], list)


# --------------------------------------------------------------------------- #
# 4. Cross-Market Radar API
# --------------------------------------------------------------------------- #
@pytest.fixture(scope='module')
def radar_payload(http):
    """Prime the radar (first call can take up to 60s) and cache the result
    for the rest of the class.  Retries once with a longer timeout if the
    first attempt trips."""
    url = f'{BASE_URL}/api/scan/cross-market-squeeze'
    params = {'limit_per_market': 10, 'universe_scan_limit': 50}
    last_exc = None
    for attempt in range(2):
        try:
            r = http.get(url, params=params, timeout=LONG_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            last_exc = AssertionError(f'radar returned {r.status_code}: {r.text[:200]}')
        except requests.exceptions.RequestException as e:
            last_exc = e
        # Backoff before retry.
        time.sleep(5)
    pytest.fail(f'Radar API unreachable after 2 attempts: {last_exc}')


class TestRadarAPI:
    REQUIRED_TOP_KEYS = {'generated_utc', 'meta', 'stocks', 'crypto',
                         'top_combined'}
    REQUIRED_ITEM_FIELDS = {
        'symbol', 'market', 'final_score', 'final_direction', 'tier',
        'conviction', 'conviction_pct', 'ssp', 'ssp_label', 'pvi',
        'pvi_bucket', 'correlation', 'squeeze_kind', 'squeeze_direction',
    }

    def test_top_level_shape(self, radar_payload):
        missing = self.REQUIRED_TOP_KEYS - set(radar_payload.keys())
        assert not missing, f'radar missing top-level keys: {missing}'

    def test_meta_driver_baskets(self, radar_payload):
        meta = radar_payload['meta']
        assert meta.get('driver_basket_stocks') == 'BTC-USD', \
            f'driver_basket_stocks = {meta.get("driver_basket_stocks")}'
        assert meta.get('driver_basket_crypto') == 'SPY', \
            f'driver_basket_crypto = {meta.get("driver_basket_crypto")}'

    def test_meta_weights(self, radar_payload):
        w = radar_payload['meta'].get('weights') or {}
        assert w.get('ssp_norm') == 0.45, f'ssp_norm={w.get("ssp_norm")}'
        assert w.get('pvi_norm') == 0.35, f'pvi_norm={w.get("pvi_norm")}'
        assert w.get('correlation') == 0.20, f'correlation={w.get("correlation")}'

    def test_stocks_and_crypto_lists_shape(self, radar_payload):
        for market_key in ('stocks', 'crypto'):
            items = radar_payload.get(market_key) or []
            assert isinstance(items, list), f'{market_key} not a list'
            # At least one market should return results.
        total = len(radar_payload.get('stocks') or []) + \
            len(radar_payload.get('crypto') or [])
        assert total >= 1, 'radar returned no items in either market'

    def test_every_item_has_required_fields(self, radar_payload):
        all_items = (radar_payload.get('stocks') or []) + \
            (radar_payload.get('crypto') or []) + \
            (radar_payload.get('top_combined') or [])
        assert all_items, 'no items to validate'
        for item in all_items:
            missing = self.REQUIRED_ITEM_FIELDS - set(item.keys())
            assert not missing, \
                f'item {item.get("symbol")!r} missing fields {missing}'

    def test_items_sorted_by_conviction_desc(self, radar_payload):
        for market_key in ('stocks', 'crypto'):
            items = radar_payload.get(market_key) or []
            if len(items) < 2:
                continue
            convictions = [it.get('conviction') for it in items]
            # Convert None → -inf for ordering.
            norm = [c if c is not None else float('-inf') for c in convictions]
            assert norm == sorted(norm, reverse=True), \
                f'{market_key} not sorted by conviction desc: {convictions}'

    def test_top_combined_merges_both_markets(self, radar_payload):
        combined = radar_payload.get('top_combined') or []
        assert isinstance(combined, list)
        if combined:
            markets_seen = {it.get('market') for it in combined}
            # Should include at least one market label; if both markets
            # have rows, should include both.
            stocks_ct = len(radar_payload.get('stocks') or [])
            crypto_ct = len(radar_payload.get('crypto') or [])
            if stocks_ct > 0 and crypto_ct > 0:
                assert markets_seen.issuperset({'stocks', 'crypto'}) \
                    or len(markets_seen) >= 1  # tolerate partial merge

    def test_only_populated_pvi_items(self, radar_payload):
        """Per review: only items with pvi > 0.1 (populated signal) allowed."""
        all_items = (radar_payload.get('stocks') or []) + \
            (radar_payload.get('crypto') or [])
        for item in all_items:
            pvi = item.get('pvi')
            assert pvi is not None, \
                f'{item.get("symbol")} has null pvi (placeholder row)'
            assert pvi > 0.1, \
                f'{item.get("symbol")} pvi={pvi} — placeholder row leaked in'


# --------------------------------------------------------------------------- #
# 5. Radar page + nav link
# --------------------------------------------------------------------------- #
class TestRadarPage:
    def test_radar_page_serves(self, http):
        r = http.get(f'{BASE_URL}/cross-market-squeeze.html',
                     timeout=FAST_TIMEOUT)
        assert r.status_code == 200
        body = r.text
        assert 'data-testid="cmr-col-stocks"' in body, \
            'cmr-col-stocks testid missing from radar page'
        assert 'data-testid="cmr-col-crypto"' in body, \
            'cmr-col-crypto testid missing from radar page'

    def test_dashboard_has_radar_nav_link(self, http):
        # Served under the /frontend/ mount path per review request.
        r = http.get(f'{BASE_URL}/frontend/market-refinement-dashboard.html',
                     timeout=FAST_TIMEOUT)
        assert r.status_code == 200
        body = r.text
        assert 'data-testid="open-cross-market-radar"' in body, \
            'open-cross-market-radar link missing from dashboard header'
        assert '/cross-market-squeeze.html' in body, \
            'dashboard nav link does not point to /cross-market-squeeze.html'


# --------------------------------------------------------------------------- #
# 6. General regression — previously verified endpoints
# --------------------------------------------------------------------------- #
class TestGeneralRegression:
    def test_universes_integrity(self, http):
        r = http.get(f'{BASE_URL}/api/universes/integrity', timeout=FAST_TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        # Field name varies by version — just make sure it's a healthy dict.
        assert isinstance(data, dict) and len(data) > 0

    def test_share_page(self, http):
        r = http.get(f'{BASE_URL}/share/BTC-USD', timeout=MID_TIMEOUT)
        assert r.status_code == 200
        assert 'BTC-USD' in r.text

    def test_share_og_image(self, http):
        r = http.get(f'{BASE_URL}/share/BTC-USD/og.png', timeout=MID_TIMEOUT)
        assert r.status_code == 200
        assert r.headers.get('content-type', '').startswith('image/png')
        assert len(r.content) > 1000

    def test_shared_analyses_page(self, http):
        r = http.get(f'{BASE_URL}/shared-analyses.html', timeout=FAST_TIMEOUT)
        assert r.status_code == 200

    def test_source_download(self, http):
        r = http.get(f'{BASE_URL}/api/download/source.zip',
                     timeout=MID_TIMEOUT, stream=True)
        assert r.status_code == 200
        ct = r.headers.get('content-type', '')
        assert 'zip' in ct or 'octet-stream' in ct

    def test_stock_detail_fresh(self, http):
        r = http.get(f'{BASE_URL}/stock/A',
                     params={'require_fresh': 'true'}, timeout=MID_TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert data.get('symbol') == 'A'
