"""Regression + bug-fix verification tests for the 3 bugs reported in session:
  1. Universe schema hardening + auto-restore from baseline
  2. Filter-elevated stocks with incomplete metrics -> require_fresh=true detail path
  3. Refresh button label change + loadDetail passes require_fresh=true
Also runs the regression set for the earlier session's features.
"""
import json
import os
import pathlib
import subprocess
import time

import pytest
import requests

BASE_URL = "http://localhost:8001"
DATA_DIR = pathlib.Path("/app/data")
ACTIVE_FILE = DATA_DIR / "active_universes.json"
BASELINE_FILE = DATA_DIR / "active_universes.baseline.json"


@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------------- BUG FIX 1: Universe integrity + schema hardening ----------------
class TestUniverseIntegrity:
    def test_integrity_endpoint_healthy(self, api):
        r = api.get(f"{BASE_URL}/api/universes/integrity", timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("schema_version") == 2
        assert d.get("stocks_active") == 22
        assert d.get("crypto_active") == 2
        assert d.get("stocks_healthy") is True
        assert d.get("crypto_healthy") is True
        assert d.get("baseline_present") is True
        assert d.get("baseline_stocks") == 22
        assert d.get("baseline_crypto") == 2

    def test_active_file_has_schema_version(self):
        data = json.loads(ACTIVE_FILE.read_text())
        assert data.get("schema_version") == 2
        assert "updated_at_utc" in data
        assert isinstance(data.get("stocks"), list)
        assert isinstance(data.get("crypto"), list)

    def test_baseline_file_exists_and_versioned(self):
        assert BASELINE_FILE.exists()
        data = json.loads(BASELINE_FILE.read_text())
        assert data.get("schema_version") == 2
        assert "captured_at_utc" in data
        assert len(data.get("stocks", [])) == 22
        assert len(data.get("crypto", [])) == 2


class TestUniverseAutoRestore:
    """Simulate truncation: write stub → restart backend → confirm auto-restore + warning log."""

    def test_truncation_recovery(self, api):
        # Snapshot originals so we can restore on failure
        orig_active = ACTIVE_FILE.read_text()
        orig_baseline = BASELINE_FILE.read_text()
        try:
            # 1. Write a truncated stub
            stub = {"schema_version": 2, "stocks": ["leveraged"], "crypto": []}
            ACTIVE_FILE.write_text(json.dumps(stub))

            # 2. Restart backend
            subprocess.run(
                ["sudo", "supervisorctl", "restart", "backend"],
                check=True, timeout=30, capture_output=True,
            )

            # 3. Wait for backend to come back
            deadline = time.time() + 30
            up = False
            while time.time() < deadline:
                try:
                    r = requests.get(f"{BASE_URL}/api/universes/integrity", timeout=3)
                    if r.status_code == 200:
                        up = True
                        break
                except Exception:
                    pass
                time.sleep(1)
            assert up, "backend did not come back up within 30s"
            time.sleep(3)  # give restore path a beat to run

            # 4. Verify stocks_active restored to 22
            r = requests.get(f"{BASE_URL}/api/universes/integrity", timeout=10)
            d = r.json()
            assert d.get("stocks_active") == 22, f"expected 22, got {d}"
            assert d.get("crypto_active") == 2
            assert d.get("stocks_healthy") is True
            assert d.get("crypto_healthy") is True

            # 5. Check WARNING line
            log_path = "/var/log/supervisor/backend.err.log"
            try:
                with open(log_path, "r") as f:
                    log_tail = f.read()[-8000:]
            except Exception:
                log_tail = ""
            assert "active_universes.json stocks list truncated" in log_tail, (
                "expected WARNING 'active_universes.json stocks list truncated' in backend.err.log; "
                f"tail: {log_tail[-500:]}"
            )
        finally:
            # Best-effort: if disk got left in a bad state, restore baseline
            try:
                data = json.loads(ACTIVE_FILE.read_text())
                if len(data.get("stocks", [])) < 5:
                    ACTIVE_FILE.write_text(orig_active)
                    BASELINE_FILE.write_text(orig_baseline)
                    subprocess.run(["sudo", "supervisorctl", "restart", "backend"],
                                   timeout=30, capture_output=True)
                    time.sleep(3)
            except Exception:
                pass


# ---------------- BUG FIX 2/3: Detail fresh-rescore path for filter-elevated stocks ----
class TestDetailFreshPath:
    KEY_FACTORS = [
        "trend_volume_delta",
        "institutional_confluence",
        "options_positioning",
        "volume_sentiment",
        "short_selling_pressure",
        "predicted_volume_intensity",
        "options_expiration",
    ]

    def test_zm_require_fresh_returns_fresh_or_live_source(self, api):
        r = api.get(
            f"{BASE_URL}/stock/ZM?market=stocks&require_fresh=true",
            timeout=90,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        # Acceptable outcomes per task spec:
        #  (a) detail_source is not 'snapshot_mirror' — require_fresh path exercised
        #  (b) provider_outcome='cache_after_live_failed' if providers down
        assert d.get("detail_source") != "snapshot_mirror", (
            f"require_fresh should bypass snapshot_mirror; got detail_source={d.get('detail_source')}"
        )
        fresh = d.get("fresh_rescore") is True
        ds = d.get("data_source")
        po = d.get("provider_outcome")
        assert (
            fresh
            or ds in ("yfinance-detail", "yahoo-chart", "cache-detail")
            or po == "cache_after_live_failed"
        ), f"unexpected detail state: fresh_rescore={fresh} data_source={ds} provider_outcome={po}"

        # Factor-breakdown check: require >= 5 of 7 keys with non-null score
        fb = d.get("factor_breakdown", {}) or {}
        market = fb.get("market", {}) or {}
        present = 0
        for k in self.KEY_FACTORS:
            v = market.get(k)
            if isinstance(v, dict):
                if v.get("score") is not None:
                    present += 1
            elif v is not None:
                present += 1
        # If all providers are down the spec allows leniency, so accept >=5
        assert present >= 5, (
            f"expected >=5 of {self.KEY_FACTORS} present in factor_breakdown.market; got {present}. "
            f"market keys: {list(market.keys())}"
        )

    def test_snapshot_mirror_legacy_fast_path(self, api):
        # Try a symbol known to be in current scan batch
        candidates = ["A", "ABBV", "AAPL", "MSFT"]
        chosen = None
        payload = None
        for sym in candidates:
            r = api.get(f"{BASE_URL}/stock/{sym}?market=stocks", timeout=60)
            if r.status_code == 200:
                d = r.json()
                if d.get("detail_source") == "snapshot_mirror":
                    chosen = sym
                    payload = d
                    break
        if not chosen:
            pytest.skip(
                "No snapshot_mirror candidate available (scan batch may be warming up)"
            )
        assert payload.get("detail_source") == "snapshot_mirror"
        assert payload.get("symbol", "").upper() == chosen

    def test_detail_source_field_type_valid(self, api):
        r = api.get(f"{BASE_URL}/stock/ZM?market=stocks&require_fresh=true", timeout=90)
        assert r.status_code == 200
        d = r.json()
        ds = d.get("detail_source", "MISSING")
        # Field must exist (may be None on fresh path)
        assert ds in ("snapshot_mirror", None) or ds == "MISSING" and False, (
            f"detail_source must be one of ['snapshot_mirror', None], got {ds!r}"
        )


# ---------------- BUG FIX 3: Frontend label + require_fresh wiring (via grep) ----
class TestFrontendWiring:
    APP_JS = pathlib.Path("/app/frontend/app.js")

    def test_load_detail_uses_require_fresh(self):
        text = self.APP_JS.read_text()
        assert "require_fresh=true" in text, "loadDetail should pass require_fresh=true"

    def test_refresh_button_label_updated(self):
        text = self.APP_JS.read_text()
        assert "Force refresh" in text
        assert "Unblock refresh" in text
        # title/tooltip mentions lockup wording
        assert "lockup" in text.lower() or "rate-limit" in text.lower()


# ---------------- Regression: manual refresh POST route still works ----------------
class TestManualRefreshRoute:
    def test_manual_refresh_returns_row(self, api):
        r = api.post(f"{BASE_URL}/stock/AAPL/refresh?market=stocks", timeout=90)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "symbol" in d
        assert d["symbol"].upper() == "AAPL"
        # refresh_failed field may or may not be present depending on provider outcome
        assert "final_score" in d or "score" in d or "composite_score" in d


# ---------------- Regression: universes list + toggle ----------------
class TestUniversesListToggle:
    def test_list_stocks_22_and_crypto_2(self, api):
        r_s = api.get(f"{BASE_URL}/api/universes?market=stocks", timeout=15)
        assert r_s.status_code == 200
        s = r_s.json()
        assert s.get("active_count") == 22
        assert isinstance(s.get("universes"), list)
        assert len(s["universes"]) == 22

        r_c = api.get(f"{BASE_URL}/api/universes?market=crypto", timeout=15)
        assert r_c.status_code == 200
        c = r_c.json()
        assert c.get("active_count") == 2
        assert len(c["universes"]) == 2

    def test_toggle_persists(self, api):
        # Pick a non-critical stock group to toggle off then back on
        target = "nyse_arca_5_5"
        # Toggle off
        r1 = api.post(
            f"{BASE_URL}/api/universes/toggle",
            json={"market": "stocks", "key": target, "active": False},
            timeout=15,
        )
        assert r1.status_code == 200, r1.text
        disk_after_off = json.loads(ACTIVE_FILE.read_text())
        assert target not in disk_after_off["stocks"]

        # Toggle on
        r2 = api.post(
            f"{BASE_URL}/api/universes/toggle",
            json={"market": "stocks", "key": target, "active": True},
            timeout=15,
        )
        assert r2.status_code == 200, r2.text
        disk_after_on = json.loads(ACTIVE_FILE.read_text())
        assert target in disk_after_on["stocks"]

        # Baseline should still reflect the healthy 22 groups
        b = json.loads(BASELINE_FILE.read_text())
        assert len(b["stocks"]) == 22
        assert len(b["crypto"]) == 2


# ---------------- Regression: earlier-session features ----------------
class TestPriorSessionFeatures:
    def test_share_symbol_html(self, api):
        r = api.get(f"{BASE_URL}/share/A", timeout=15)
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "").lower()

    def test_share_symbol_og_png(self, api):
        r = api.get(f"{BASE_URL}/share/A/og.png", timeout=30)
        assert r.status_code == 200
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_shares_recent(self, api):
        r = api.get(f"{BASE_URL}/api/shares/recent", timeout=15)
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d, dict) or isinstance(d, list)

    def test_shared_analyses_page(self, api):
        r = api.get(f"{BASE_URL}/shared-analyses.html", timeout=15)
        assert r.status_code == 200
        body = r.text
        assert "Download" in body or "source" in body.lower()

    def test_source_zip(self, api):
        r = api.get(f"{BASE_URL}/api/download/source.zip", timeout=60)
        assert r.status_code == 200
        assert r.content[:2] == b"PK"

    def test_market_refinement_dashboard_has_f_p_ctx_column(self, api):
        r = api.get(f"{BASE_URL}/market-refinement-dashboard.html", timeout=15)
        assert r.status_code == 200
        assert "F-P(ctx)" in r.text

    def test_squeeze_watch_preset_present(self, api):
        r = api.get(f"{BASE_URL}/", timeout=15)
        # squeeze-watch preset chip should appear inside index HTML or app.js
        found = "squeeze-watch" in r.text or "Squeeze watch" in r.text
        if not found:
            app_js = pathlib.Path("/app/frontend/app.js").read_text()
            found = "squeeze-watch" in app_js or "Squeeze watch" in app_js
        assert found, "squeeze-watch preset chip not found in root html or app.js"


class TestStocksResults:
    def test_results_limit_10_returns_rows(self, api):
        r = api.get(f"{BASE_URL}/stocks/results?limit=10", timeout=30)
        assert r.status_code == 200
        d = r.json()
        rows = d.get("results") or d.get("rows") or []
        # Warmup edge-case: allow 0 rows but log
        if not rows:
            pytest.skip(f"results empty (warmup?) payload keys: {list(d.keys())}")
        assert len(rows) <= 10
        # At least one row has real symbol + non-zero final_score
        has_real = False
        for row in rows:
            sym = row.get("symbol")
            score = row.get("final_score") or row.get("score") or row.get("composite_score")
            if sym and score:
                try:
                    if float(score) != 0.0:
                        has_real = True
                        break
                except Exception:
                    continue
        assert has_real, f"no row had non-zero final_score; sample: {rows[0]}"
