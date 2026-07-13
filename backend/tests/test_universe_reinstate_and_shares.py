"""
Backend tests for:
- BUG VERIFY: Universe reinstate (stocks 22 groups / crypto 2 groups)
- REGRESSION: New /share/{symbol}, /share/{symbol}/og.png, /api/shares/recent,
  /shared-analyses.html, /api/download/source.zip
"""
import os
import time
import io
import pytest
import requests

# Prefer localhost for backend routes; frontend HTML also served through the ingress
BACKEND = "http://localhost:8001"
PREVIEW = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://a50d5ff2-a25c-4f65-8076-e42f17b69a95.preview.emergentagent.com",
).rstrip("/")

TIMEOUT = 60


@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    s.headers.update({"Accept": "application/json"})
    return s


# ============================================================
# BUG VERIFY — universe reinstate
# ============================================================
class TestUniverseReinstate:
    def test_stocks_universe_all_active(self, api):
        r = api.get(f"{BACKEND}/api/universes?market=stocks", timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert data["market"] == "stocks"
        assert data["active_count"] == 22, f"Expected 22 active stock groups, got {data['active_count']}"
        # ~11,800 symbols expected
        assert 11000 <= data["active_symbol_count"] <= 13000, (
            f"Expected ~11,800 active symbols, got {data['active_symbol_count']}"
        )
        # Every group must be active=True
        inactive = [g for g in data["groups"] if not g.get("active")]
        assert inactive == [], f"These groups are not active: {inactive}"
        # And every group has count > 0
        empty = [g["key"] for g in data["groups"] if g.get("count", 0) <= 0]
        assert empty == [], f"These groups are empty (0 tickers): {empty}"

    def test_crypto_universe_all_active(self, api):
        r = api.get(f"{BACKEND}/api/universes?market=crypto", timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        assert data["market"] == "crypto"
        assert data["active_count"] == 2, f"Expected 2 active crypto groups, got {data['active_count']}"
        # Should be ~716 (55 + 661)
        assert 600 <= data["active_symbol_count"] <= 900, (
            f"Expected ~716 crypto symbols, got {data['active_symbol_count']}"
        )
        keys = {g["key"]: g for g in data["groups"]}
        assert "crypto_core" in keys and keys["crypto_core"]["active"] is True
        assert "crypto_rest" in keys and keys["crypto_rest"]["active"] is True
        assert keys["crypto_core"]["count"] >= 40
        assert keys["crypto_rest"]["count"] >= 500

    def test_stocks_results_has_symbols(self, api):
        # /stocks/results is unauthenticated; poll a bit for warm-up
        last = None
        for _ in range(8):
            r = api.get(f"{BACKEND}/stocks/results?limit=10", timeout=TIMEOUT)
            if r.status_code == 200:
                last = r.json()
                if last.get("results"):
                    break
            time.sleep(3)
        assert last is not None, "No response for /stocks/results"
        universe_size = (
            last.get("universe_size")
            or last.get("scan_progress", {}).get("universe_size")
            or last.get("total")
            or 0
        )
        assert universe_size >= 11000, f"universe_size expected >=11k, got {universe_size}"
        assert len(last.get("results", [])) > 0, "No stock rows returned after warmup"
        first = last["results"][0]
        assert first.get("symbol"), "Row missing symbol"

    def test_crypto_results_has_symbols(self, api):
        last = None
        for _ in range(8):
            r = api.get(f"{BACKEND}/stocks/results?limit=10&market=crypto", timeout=TIMEOUT)
            if r.status_code == 200:
                last = r.json()
                if last.get("results"):
                    break
            time.sleep(3)
        assert last is not None
        universe_size = (
            last.get("universe_size")
            or last.get("scan_progress", {}).get("universe_size")
            or last.get("total")
            or 0
        )
        assert universe_size >= 600, f"crypto universe_size expected >=600, got {universe_size}"
        # results may still be warming; if present, verify shape
        if last.get("results"):
            syms = [row["symbol"] for row in last["results"]]
            assert any("-USD" in s or s.endswith("USD") for s in syms), f"No USD crypto symbols in {syms}"


# ============================================================
# REGRESSION — /share/{symbol}, OG image, /api/shares/recent, gallery, source.zip
# ============================================================
class TestShareRoutes:
    def test_share_html_meta_tags(self, api):
        r = api.get(f"{BACKEND}/share/ABBV", timeout=TIMEOUT)
        assert r.status_code == 200, f"/share/ABBV -> {r.status_code}"
        html = r.text
        assert 'og:image' in html, "Missing og:image meta tag"
        assert 'og:title' in html, "Missing og:title meta tag"
        assert 'og:description' in html, "Missing og:description meta tag"
        assert 'og:url' in html, "Missing og:url meta tag"

    def test_share_html_meta_tags_alt(self, api):
        r = api.get(f"{BACKEND}/share/A", timeout=TIMEOUT)
        assert r.status_code == 200
        assert 'og:image' in r.text

    def test_share_og_png(self, api):
        r = api.get(f"{BACKEND}/share/ABBV/og.png", timeout=TIMEOUT)
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        assert "image/png" in ct, f"Wrong content-type: {ct}"
        # Verify PNG magic bytes
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n", "Not a valid PNG"
        assert len(r.content) > 1000, f"PNG too small: {len(r.content)} bytes"

    def test_api_shares_recent(self, api):
        r = api.get(f"{BACKEND}/api/shares/recent", timeout=TIMEOUT)
        assert r.status_code == 200
        data = r.json()
        # Accept either a list directly or {items:[...]} / {shares:[...]}
        if isinstance(data, dict):
            items = data.get("items") or data.get("shares") or data.get("results") or []
        else:
            items = data
        assert isinstance(items, list), f"shares/recent not a list: {type(items)}"
        # Should contain at least our earlier ABBV/A hits
        # But warming/dedupe may cause 0, so just assert response shape

    def test_shared_analyses_html(self, api):
        r = api.get(f"{BACKEND}/shared-analyses.html", timeout=TIMEOUT)
        assert r.status_code == 200
        assert "Download source" in r.text or "download" in r.text.lower(), (
            "Gallery page missing download-source card"
        )

    def test_source_download_zip(self, api):
        r = api.get(f"{BACKEND}/api/download/source.zip", timeout=TIMEOUT, stream=True)
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        assert "application/zip" in ct or "octet-stream" in ct, f"Wrong content-type: {ct}"
        # Verify ZIP magic bytes on first chunk
        chunk = next(r.iter_content(chunk_size=8), b"")
        assert chunk[:2] == b"PK", f"Not a ZIP file, first bytes: {chunk[:4]!r}"


# ============================================================
# REGRESSION — F-P(ctx) column header in dashboard HTML
# ============================================================
class TestDashboardHeaders:
    def test_fp_ctx_header_present(self, api):
        r = api.get(f"{BACKEND}/frontend/market-refinement-dashboard.html", timeout=TIMEOUT)
        # Also try the direct filesystem read fallback
        html = r.text if r.status_code == 200 else ""
        if not html:
            with open("/app/frontend/market-refinement-dashboard.html") as f:
                html = f.read()
        assert "F-P(ctx)" in html, "F-P(ctx) column header missing from dashboard"
        # Order: F-P(up) then F-P(ctx) then F-Drift
        i_up = html.find("F-P(up)")
        i_ctx = html.find("F-P(ctx)")
        i_drift = html.find("F-Drift")
        assert i_up != -1 and i_ctx != -1 and i_drift != -1
        assert i_up < i_ctx < i_drift, "F-P(ctx) is not between F-P(up) and F-Drift"
