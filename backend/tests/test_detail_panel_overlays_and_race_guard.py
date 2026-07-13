"""Phase 26.71 hotfix verification suite.

Verifies:
- BUG FIX 1 (backend): require_fresh=true returns detail rows with
  forward_metrics / advanced_signals / lab_signals / strategy_signals
  overlays for both the snapshot-refreshed path (stocks) and the rescore
  path (any symbol without a healthy snapshot).  For crypto, the KEYS
  must be present in the response (values may be None if daily-history
  is thin).
- BUG FIX 2 (frontend, static grep): loadDetail contains the race
  guard `if (state.selectedSymbol && state.selectedSymbol !== symbol)`.
- Regressions: snapshot_mirror path, force_live path, /stocks/results,
  universes integrity, cross-market squeeze radar.
"""
import os
import re

import pytest
import requests

BASE_URL = os.environ.get(
    'BACKEND_TEST_BASE_URL', 'http://localhost:8001'
).rstrip('/')

REQUIRED_OVERLAY_BLOCKS = [
    'forward_metrics', 'advanced_signals', 'lab_signals', 'strategy_signals',
]
FLAT_CONTEXT_FIELDS = [
    'short_selling_pressure_score', 'predicted_volume_intensity_score',
    'days_to_options_expiration', 'expiration_risk_flag',
]


@pytest.fixture(scope='module')
def api():
    session = requests.Session()
    session.headers.update({'Content-Type': 'application/json'})
    return session


# --- BUG FIX 1: stocks require_fresh --------------------------------------
class TestRequireFreshStocks:
    def test_A_require_fresh_has_all_overlays(self, api):
        r = api.get(f'{BASE_URL}/stock/A', params={'market': 'stocks', 'require_fresh': 'true'}, timeout=90)
        assert r.status_code == 200
        d = r.json()
        for k in REQUIRED_OVERLAY_BLOCKS:
            assert k in d, f'missing top-level key: {k}'
            assert d.get(k) is not None, f'{k} is None for stocks require_fresh'
        # forward_1d must expose the CF + context probability + Kelly rank fields
        fm1d = (d['forward_metrics'] or {}).get('forward_1d') or {}
        for k in ('p_up_cf', 'p_up_ctx', 'effective_kelly_rank'):
            assert k in fm1d, f'forward_1d missing {k}'

    def test_A_detail_source_not_snapshot_mirror_under_require_fresh(self, api):
        r = api.get(f'{BASE_URL}/stock/A', params={'market': 'stocks', 'require_fresh': 'true'}, timeout=90)
        assert r.status_code == 200
        d = r.json()
        # detail_source must be 'snapshot_refreshed' (healthy snapshot) OR
        # None (rescore path when snapshot is incomplete).  It must NOT be
        # 'snapshot_mirror' — that would mean quote refresh didn't run.
        assert d.get('detail_source') != 'snapshot_mirror', \
            f'detail_source={d.get("detail_source")} but require_fresh=true was sent'

    def test_flat_scanner_context_fields_present(self, api):
        r = api.get(f'{BASE_URL}/stock/A', params={'market': 'stocks', 'require_fresh': 'true'}, timeout=90)
        assert r.status_code == 200
        d = r.json()
        for k in FLAT_CONTEXT_FIELDS:
            assert k in d, f'flat field {k} missing at row root'


# --- BUG FIX 1: crypto require_fresh --------------------------------------
class TestRequireFreshCrypto:
    def test_btc_require_fresh_overlay_keys_present(self, api):
        r = api.get(f'{BASE_URL}/stock/BTC-USD', params={'market': 'crypto', 'require_fresh': 'true'}, timeout=90)
        assert r.status_code == 200
        d = r.json()
        # Per review request: KEYS must exist even if values may be None
        # for coins with insufficient daily history.
        missing = [k for k in REQUIRED_OVERLAY_BLOCKS if k not in d]
        assert not missing, f'crypto require_fresh missing overlay keys: {missing}'


# --- BUG FIX 1: rescore path (force_live) also emits overlays -------------
class TestForceLivePathOverlays:
    def test_force_live_stocks_has_overlays(self, api):
        # POST /stock/{symbol}/refresh forces the full rescore path
        r = api.post(f'{BASE_URL}/stock/A/refresh', params={'market': 'stocks'}, timeout=120)
        assert r.status_code == 200
        d = r.json()
        for k in REQUIRED_OVERLAY_BLOCKS:
            assert k in d, f'force_live path missing overlay key: {k}'
            assert d.get(k) is not None, f'force_live path {k} is None'
        # Flat fields also populated on the rescore path
        for k in FLAT_CONTEXT_FIELDS:
            assert k in d, f'force_live flat field {k} missing'


# --- BUG FIX 2: frontend race guard (static grep) -------------------------
class TestFrontendRaceGuard:
    def test_loadDetail_race_guard_present(self):
        path = '/app/frontend/app.js'
        assert os.path.exists(path), f'{path} not found'
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()
        # Exact guard from the phase 26.71 hotfix
        pattern = r'if\s*\(\s*state\.selectedSymbol\s*&&\s*state\.selectedSymbol\s*!==\s*symbol\s*\)\s*\{'
        assert re.search(pattern, src), \
            'loadDetail race-guard `if (state.selectedSymbol && state.selectedSymbol !== symbol)` not found'


# --- Regression: default snapshot_mirror path -----------------------------
class TestRegressionSnapshotMirror:
    def test_no_require_fresh_uses_snapshot_mirror(self, api):
        # Try a batch of common symbols — most should be in the healthy
        # snapshot pool and return `snapshot_mirror`.  We accept success
        # if ANY of them mirrors AND that mirrored row has the overlay
        # bundle.  (Symbols not yet batch-scored return `None` via the
        # rescore path — that's not the mirror regression we're testing.)
        mirror_hits = []
        for sym in ('AACG', 'AACB', 'AAPL', 'A', 'AACI', 'AACBR'):
            r = api.get(f'{BASE_URL}/stock/{sym}', params={'market': 'stocks'}, timeout=45)
            if r.status_code != 200:
                continue
            d = r.json()
            if d.get('detail_source') == 'snapshot_mirror':
                mirror_hits.append((sym, d))
                # Must carry every overlay
                for k in REQUIRED_OVERLAY_BLOCKS:
                    assert k in d and d[k] is not None, \
                        f'{sym} snapshot_mirror row missing overlay: {k}'
                break
        assert mirror_hits, \
            'no symbol returned detail_source=snapshot_mirror — batch snapshot pool may be cold'


# --- Regression: /stocks/results ------------------------------------------
class TestRegressionStocksResults:
    def test_stocks_results_returns_valid_rows(self, api):
        r = api.get(f'{BASE_URL}/stocks/results', params={'limit': 10}, timeout=60)
        assert r.status_code == 200
        d = r.json()
        rows = d.get('results') or d.get('rows') or []
        assert len(rows) > 0, 'stocks/results returned zero rows'
        for row in rows[:5]:
            assert 'symbol' in row
            assert 'final_score' in row


# --- Regression: universes integrity + cross-market radar -----------------
class TestRegressionUniverseAndRadar:
    def test_universes_integrity(self, api):
        r = api.get(f'{BASE_URL}/api/universes/integrity', timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert d.get('stocks_healthy') is True
        assert d.get('crypto_healthy') is True

    def test_cross_market_squeeze_radar(self, api):
        r = api.get(
            f'{BASE_URL}/api/scan/cross-market-squeeze',
            params={'limit_per_market': 5, 'universe_scan_limit': 30},
            timeout=180,
        )
        assert r.status_code == 200
        d = r.json()
        for k in ('generated_utc', 'meta', 'stocks', 'crypto', 'top_combined'):
            assert k in d, f'radar payload missing key: {k}'
