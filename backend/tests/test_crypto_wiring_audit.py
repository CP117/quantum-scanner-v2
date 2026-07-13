"""Iteration 3: Crypto wiring audit — verify every algo/scanner/filter/metric
works for the crypto universe (not just stocks), plus stock-regression checks.

Testing the fixes documented in the review request:
- _load_market_proxy_returns(market) uses SPY for stocks, BTC-USD for crypto
- attach_forward_metrics_fast/garch propagate market
- scoring_service + top10_priority_service infer market from -USD suffix
- social_share / og_image_service fall back to get_symbol_detail
"""
from __future__ import annotations

import io
import os
import re
import time
import pytest
import requests

BASE_URL = os.environ.get("BACKEND_URL_OVERRIDE", "http://localhost:8001").rstrip("/")
TIMEOUT = 60  # crypto fetches can be slow on fresh boot


@pytest.fixture(scope="module")
def http():
    s = requests.Session()
    s.headers.update({"Accept": "application/json"})
    return s


# ---------------------------------------------------------------------------
# Module-scoped fetch of crypto snapshot (reused across many tests)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def crypto_snapshot(http):
    """Fetch /stocks/results?market=crypto&limit=10 once for the module."""
    r = http.get(f"{BASE_URL}/stocks/results", params={"market": "crypto", "limit": 10}, timeout=TIMEOUT)
    assert r.status_code == 200, f"crypto snapshot failed: {r.status_code} {r.text[:300]}"
    data = r.json()
    results = data.get("results") or []
    return data, results


@pytest.fixture(scope="module")
def stock_snapshot(http):
    r = http.get(f"{BASE_URL}/stocks/results", params={"limit": 10}, timeout=TIMEOUT)
    assert r.status_code == 200
    data = r.json()
    return data, data.get("results") or []


# ---------------------------------------------------------------------------
# CRYPTO WIRING — factor families
# ---------------------------------------------------------------------------
class TestCryptoWiring:
    """Verify crypto snapshot has all expected wiring."""

    def test_snapshot_returns_multiple_crypto_rows(self, crypto_snapshot):
        _, results = crypto_snapshot
        assert len(results) >= 5, f"expected >=5 crypto rows, got {len(results)}"
        symbols = {r.get("symbol") for r in results}
        # BTC + ETH must be present
        assert "BTC-USD" in symbols, f"BTC-USD missing from crypto snapshot; got {symbols}"
        assert "ETH-USD" in symbols, f"ETH-USD missing from crypto snapshot; got {symbols}"

    def test_factor_families_present(self, crypto_snapshot):
        """Each crypto row must have >=6 of the 8 factor families populated
        under factor_breakdown.market. options_expiration key must EXIST even
        if UNAVAILABLE for crypto."""
        _, results = crypto_snapshot
        expected = [
            "trend_volume_delta", "institutional_confluence", "options_positioning",
            "volume_sentiment", "short_selling_pressure", "predicted_volume_intensity",
            "options_expiration", "reaction_map",
        ]
        for row in results:
            fb = row.get("factor_breakdown") or {}
            market = fb.get("market") or {}
            present_keys = [k for k in expected if k in market]
            assert "options_expiration" in market, (
                f"{row.get('symbol')}: options_expiration KEY missing from factor_breakdown.market"
            )
            # non-null score fields for at least 6 of 8
            non_null = 0
            for k in expected:
                fam = market.get(k)
                if isinstance(fam, dict):
                    # look for any non-null score-ish field
                    if any(v is not None for kk, v in fam.items()
                           if any(t in kk.lower() for t in ("score", "value", "pct", "count", "flag", "label", "source"))):
                        non_null += 1
                elif fam is not None:
                    non_null += 1
            assert non_null >= 6, (
                f"{row.get('symbol')}: only {non_null}/8 factor families have data. "
                f"present_keys={present_keys}"
            )

    def test_forward_metrics_field_exists(self, crypto_snapshot):
        """forward_metrics field must exist at row level (may be None or
        contain forward_1d block). The KEY existence is what's audited."""
        _, results = crypto_snapshot
        for row in results:
            assert "forward_metrics" in row, (
                f"{row.get('symbol')}: 'forward_metrics' key missing entirely from row"
            )

    def test_forward_metrics_structure_when_populated(self, crypto_snapshot):
        """When forward_metrics is populated, the horizon blocks should
        have the p_up*/direction structure. Skip rows where it's None
        (acceptable per main-agent note — data-timing, not wiring)."""
        _, results = crypto_snapshot
        checked = 0
        for row in results:
            fm = row.get("forward_metrics")
            if not isinstance(fm, dict) or not fm:
                continue
            # any horizon block present is fine
            horizons = [k for k in fm if k.startswith("forward_")]
            assert horizons, f"{row.get('symbol')}: forward_metrics dict has no forward_* horizon blocks"
            # inspect first horizon block
            block = fm[horizons[0]]
            assert isinstance(block, dict)
            # at least one of the expected p_up/direction fields
            expected_any = {"p_up", "p_up_cf", "p_up_ctx", "direction", "kelly_rank"}
            assert expected_any.intersection(block.keys()), (
                f"{row.get('symbol')}: {horizons[0]} block missing p_up*/direction/kelly_rank fields; keys={list(block.keys())[:20]}"
            )
            checked += 1
        # We should have checked at least one non-BTC-non-Neutral row
        assert checked >= 1, "no crypto rows had populated forward_metrics — data-timing issue at minimum"

    def test_scanner_context_flat_fields(self, crypto_snapshot):
        """Flat scanner-context fields must exist at row root for crypto."""
        _, results = crypto_snapshot
        required_flat = [
            "short_selling_pressure_score", "short_selling_pressure_label",
            "predicted_volume_intensity_score", "predicted_volume_intensity_bucket",
            "predicted_volume_event_flag",
            "nearest_options_expiration", "days_to_options_expiration",
            "expiration_risk_flag",
        ]
        for row in results:
            missing = [k for k in required_flat if k not in row]
            assert not missing, (
                f"{row.get('symbol')}: missing flat scanner-context fields: {missing}"
            )


# ---------------------------------------------------------------------------
# CRYPTO FILTERS / PRESETS
# ---------------------------------------------------------------------------
class TestCryptoFilters:
    """Preset filters on crypto must not 500 — empty results are OK."""

    @pytest.mark.parametrize("preset", [
        "squeeze-watch", "volume-storm", "bearish-pressure", "expiration-pin",
    ])
    def test_preset_returns_200(self, http, preset):
        r = http.get(
            f"{BASE_URL}/stocks/results",
            params={"market": "crypto", "preset": preset, "limit": 10},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200, f"preset={preset} returned {r.status_code}: {r.text[:200]}"
        data = r.json()
        assert "results" in data
        assert isinstance(data["results"], list), f"preset={preset} results not a list"


# ---------------------------------------------------------------------------
# CRYPTO DETAIL — /stock/BTC-USD?require_fresh=true
# ---------------------------------------------------------------------------
class TestCryptoDetail:
    def test_btc_detail_fresh_rescore(self, http):
        r = http.get(
            f"{BASE_URL}/stock/BTC-USD",
            params={"market": "crypto", "require_fresh": "true"},
            timeout=90,
        )
        assert r.status_code == 200, f"BTC-USD detail returned {r.status_code}: {r.text[:200]}"
        data = r.json()
        # fresh_rescore=true OR detail_source=None per requirement
        fresh_or_rescore = data.get("fresh_rescore") is True or data.get("detail_source") in (None, "None")
        assert fresh_or_rescore, (
            f"BTC-USD detail: neither fresh_rescore=True nor detail_source=None. "
            f"fresh_rescore={data.get('fresh_rescore')} detail_source={data.get('detail_source')}"
        )
        assert data.get("data_source") in ("coingecko", "yfinance-detail"), (
            f"unexpected data_source: {data.get('data_source')}"
        )
        fs = data.get("final_score")
        assert isinstance(fs, (int, float)) and fs > 0, f"final_score not >0: {fs}"


# ---------------------------------------------------------------------------
# CRYPTO SHARE + OG IMAGE
# ---------------------------------------------------------------------------
class TestCryptoShare:
    def test_share_html_contains_populated_og(self, http):
        r = http.get(f"{BASE_URL}/share/BTC-USD", timeout=TIMEOUT)
        assert r.status_code == 200
        html = r.text
        og_title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        assert og_title, "og:title meta tag missing"
        title = og_title.group(1)
        # must contain the symbol
        assert "BTC-USD" in title, f"og:title missing BTC-USD: {title}"
        # must NOT be the fallback title
        assert "Quantum Market Scanner" not in title, (
            f"og:title is the fallback (Quantum Market Scanner) instead of the populated card: {title}"
        )
        # must include a direction
        assert re.search(r"\b(Bullish|Bearish|Neutral)\b", title), (
            f"og:title missing direction: {title}"
        )
        # must include Score plus a number
        assert re.search(r"Score\s+\d+(\.\d+)?", title), (
            f"og:title missing 'Score <number>': {title}"
        )

    def test_share_og_png_1200x630(self, http):
        r = http.get(f"{BASE_URL}/share/BTC-USD/og.png", timeout=TIMEOUT)
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("image/png"), (
            f"expected image/png, got {r.headers.get('content-type')}"
        )
        body = r.content
        assert len(body) > 5000, f"og.png suspiciously small: {len(body)} bytes"
        from PIL import Image
        im = Image.open(io.BytesIO(body))
        assert im.size == (1200, 630), f"expected 1200x630, got {im.size}"


# ---------------------------------------------------------------------------
# STOCK REGRESSION
# ---------------------------------------------------------------------------
class TestStockRegression:
    def test_stock_snapshot_populated(self, stock_snapshot):
        _, results = stock_snapshot
        assert len(results) >= 5, f"expected >=5 stock rows, got {len(results)}"

    def test_stock_factor_families(self, stock_snapshot):
        _, results = stock_snapshot
        expected = [
            "trend_volume_delta", "institutional_confluence", "options_positioning",
            "volume_sentiment", "short_selling_pressure", "predicted_volume_intensity",
            "options_expiration", "reaction_map",
        ]
        # Just check on the first 3 rows
        for row in results[:3]:
            market = (row.get("factor_breakdown") or {}).get("market") or {}
            non_null = 0
            for k in expected:
                fam = market.get(k)
                if isinstance(fam, dict) and any(
                    v is not None for kk, v in fam.items()
                    if any(t in kk.lower() for t in ("score", "value", "pct", "count", "flag", "label", "source"))
                ):
                    non_null += 1
                elif fam is not None:
                    non_null += 1
            assert non_null >= 6, (
                f"stock {row.get('symbol')}: only {non_null}/8 families populated"
            )

    def test_stock_detail_fresh_rescore(self, http, stock_snapshot):
        _, results = stock_snapshot
        # Prefer 'A' if available, else first row
        target = "A" if any(r.get("symbol") == "A" for r in results) else results[0].get("symbol")
        r = http.get(
            f"{BASE_URL}/stock/{target}",
            params={"require_fresh": "true"},
            timeout=90,
        )
        assert r.status_code == 200, f"/stock/{target} returned {r.status_code}"
        data = r.json()
        assert data.get("final_score", 0) > 0

    def test_stock_share_populated(self, http, stock_snapshot):
        _, results = stock_snapshot
        target = "ABBV" if any(r.get("symbol") == "ABBV" for r in results) else results[0].get("symbol")
        r = http.get(f"{BASE_URL}/share/{target}", timeout=TIMEOUT)
        assert r.status_code == 200
        og_title = re.search(r'<meta property="og:title" content="([^"]+)"', r.text)
        assert og_title, "og:title missing on stock share page"
        title = og_title.group(1)
        assert target in title, f"stock share og:title missing symbol {target}: {title}"
        assert "Quantum Market Scanner" not in title, (
            f"stock share og:title is fallback for {target}: {title}"
        )


# ---------------------------------------------------------------------------
# LCC MARKET PROXY unit-style check (imports the module directly)
# ---------------------------------------------------------------------------
class TestMarketProxyConstants:
    def test_constants_exist(self):
        from app.services import future_mode_service as fms
        assert getattr(fms, "_MARKET_PROXY_SYMBOL_STOCKS", None) == "SPY"
        assert getattr(fms, "_MARKET_PROXY_SYMBOL_CRYPTO", None) == "BTC-USD"

    def test_proxy_symbol_for_dispatch(self):
        from app.services import future_mode_service as fms
        assert fms._proxy_symbol_for("crypto") == "BTC-USD"
        assert fms._proxy_symbol_for("stocks") == "SPY"
        assert fms._proxy_symbol_for(None) == "SPY"

    def test_load_market_proxy_returns_uses_separate_cache_keys(self):
        """Calling _load_market_proxy_returns for 'crypto' vs 'stocks' should
        populate DIFFERENT cache entries in _market_proxy_cache."""
        from app.services import future_mode_service as fms
        # clear cache to be deterministic
        fms._market_proxy_cache.clear()
        # call both
        fms._load_market_proxy_returns("stocks")
        fms._load_market_proxy_returns("crypto")
        keys = set(fms._market_proxy_cache.keys())
        assert "SPY" in keys, f"stocks proxy (SPY) not cached; keys={keys}"
        assert "BTC-USD" in keys, f"crypto proxy (BTC-USD) not cached; keys={keys}"
        assert keys.__contains__("SPY") and keys.__contains__("BTC-USD"), (
            f"expected both SPY and BTC-USD cache-keys, got {keys}"
        )


# ---------------------------------------------------------------------------
# UNIVERSE INTEGRITY (regression from iteration 2)
# ---------------------------------------------------------------------------
class TestUniverseIntegrity:
    def test_integrity_healthy(self, http):
        r = http.get(f"{BASE_URL}/api/universes/integrity", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data.get("stocks_healthy") is True, f"stocks_healthy False: {data}"
        assert data.get("crypto_healthy") is True, f"crypto_healthy False: {data}"
        assert data.get("stocks_active") == 22, f"stocks_active != 22: {data.get('stocks_active')}"
        assert data.get("crypto_active") == 2, f"crypto_active != 2: {data.get('crypto_active')}"
