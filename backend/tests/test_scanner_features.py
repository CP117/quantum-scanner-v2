"""
Backend tests for Quantum Market Scanner features:
 - Short selling pressure family
 - Predicted volume intensity + PVI sort mode
 - Options expiration awareness
 - Forecast activator endpoint
 - Cache dedupe subsystem
 - /stocks/results filters (regression + new)
 - /stock/{symbol} detail regression
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fallback to preview URL declared in the review request
    BASE_URL = "https://a50d5ff2-a25c-4f65-8076-e42f17b69a95.preview.emergentagent.com"

TIMEOUT = 90


REQUIRED_ROW_FIELDS = [
    "short_selling_pressure_score",
    "short_selling_pressure_label",
    "short_selling_pressure_source",
    "predicted_volume_intensity_score",
    "predicted_volume_intensity_bucket",
    "predicted_volume_event_flag",
    "nearest_options_expiration",
    "days_to_options_expiration",
    "expiration_risk_flag",
    "future_forecast_ready",
    "future_forecast_summary",
]


@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    s.headers.update({"Accept": "application/json"})
    return s


@pytest.fixture(scope="session")
def snapshot_stocks(api):
    # Poll for warm rows (backend continuously scans)
    last = None
    for _ in range(8):
        r = api.get(f"{BASE_URL}/api/scan/snapshot", params={"market": "stocks", "limit": 20}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        last = r.json()
        if last.get("results"):
            return last
        time.sleep(4)
    return last


# ---------- Snapshot new fields ----------
class TestSnapshot:
    def test_snapshot_ok_and_shape(self, snapshot_stocks):
        d = snapshot_stocks
        assert "results" in d
        assert isinstance(d["results"], list)
        assert len(d["results"]) > 0, "no rows in snapshot (backend not warm?)"

    def test_snapshot_rows_have_new_flat_fields(self, snapshot_stocks):
        missing_report = []
        for row in snapshot_stocks["results"][:10]:
            for f in REQUIRED_ROW_FIELDS:
                if f not in row:
                    missing_report.append((row.get("symbol"), f))
        assert not missing_report, f"Missing fields in rows: {missing_report[:10]}"

    def test_snapshot_field_types(self, snapshot_stocks):
        for row in snapshot_stocks["results"][:10]:
            ssp = row.get("short_selling_pressure_score")
            pvi = row.get("predicted_volume_intensity_score")
            assert ssp is None or isinstance(ssp, (int, float))
            assert pvi is None or isinstance(pvi, (int, float))
            # expiration can legitimately be None
            dte = row.get("days_to_options_expiration")
            assert dte is None or isinstance(dte, int)
            # bucket is a string when present
            b = row.get("predicted_volume_intensity_bucket")
            assert b is None or isinstance(b, str)


# ---------- PVI sort mode ----------
class TestPviSort:
    def test_pvi_sort_mode_ordering(self, api):
        r = api.get(
            f"{BASE_URL}/api/scan/snapshot",
            params={"market": "stocks", "limit": 20, "sort": "predicted_volume_intensity"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        d = r.json()
        assert d.get("sort_mode") == "predicted_volume_intensity"
        pvis = [row.get("predicted_volume_intensity_score") or 0 for row in d["results"]]
        assert len(pvis) >= 2
        # descending
        for i in range(1, len(pvis)):
            assert pvis[i] <= pvis[i - 1] + 1e-6, f"not descending at index {i}: {pvis}"


# ---------- /stocks/results filters ----------
class TestResultsFilters:
    def test_basic_envelope(self, api):
        r = api.get(f"{BASE_URL}/stocks/results", params={"batch": 0, "limit": 25}, timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        for k in ("results", "batch", "total_batches"):
            assert k in d, f"missing key {k}"
        assert isinstance(d["results"], list)
        if d["results"]:
            # rows are normalized with new fields
            row = d["results"][0]
            for f in REQUIRED_ROW_FIELDS:
                assert f in row, f"row missing {f}"

    def test_min_pvi_filter(self, api):
        r = api.get(
            f"{BASE_URL}/stocks/results",
            params={"batch": 0, "limit": 50, "min_predicted_volume_intensity": 60},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        d = r.json()
        for row in d["results"]:
            v = row.get("predicted_volume_intensity_score")
            assert v is not None and v >= 60 - 1e-6, f"PVI {v} < 60 for {row.get('symbol')}"

    def test_pvi_bucket_filter(self, api):
        r = api.get(
            f"{BASE_URL}/stocks/results",
            params={"batch": 0, "limit": 50, "predicted_volume_intensity_bucket_in": "high,extreme"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        for row in r.json()["results"]:
            b = (row.get("predicted_volume_intensity_bucket") or "").lower()
            assert b in ("high", "extreme"), f"unexpected bucket {b} for {row.get('symbol')}"

    def test_max_dte_filter(self, api):
        r = api.get(
            f"{BASE_URL}/stocks/results",
            params={"batch": 0, "limit": 50, "max_days_to_options_expiration": 7},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        for row in r.json()["results"]:
            dte = row.get("days_to_options_expiration")
            assert dte is not None, f"{row.get('symbol')} has null dte but filter applied"
            assert dte <= 7, f"dte {dte} > 7 for {row.get('symbol')}"

    def test_short_pressure_label_filter(self, api):
        r = api.get(
            f"{BASE_URL}/stocks/results",
            params={"batch": 0, "limit": 50, "short_selling_pressure_label_in": "elevated,high,extreme"},
            timeout=TIMEOUT,
        )
        # accept 200 even with 0 rows
        assert r.status_code == 200
        for row in r.json()["results"]:
            label = (row.get("short_selling_pressure_label") or "").lower()
            assert label in ("elevated", "high", "extreme"), f"unexpected ssp label {label}"

    def test_regression_min_score_direction(self, api):
        r = api.get(
            f"{BASE_URL}/stocks/results",
            params={"batch": 0, "limit": 50, "min_score": 50, "direction": "Bullish"},
            timeout=TIMEOUT,
        )
        assert r.status_code == 200
        for row in r.json()["results"]:
            assert (row.get("final_score") or 0) >= 50 - 1e-6
            assert (row.get("final_direction") or "") == "Bullish"


# ---------- Forecast activator ----------
class TestForecastActivator:
    @pytest.fixture(scope="class")
    def symbol(self, api):
        r = api.get(f"{BASE_URL}/api/scan/snapshot", params={"market": "stocks", "limit": 20}, timeout=TIMEOUT)
        rows = r.json()["results"]
        # prefer one with future_forecast_ready
        for row in rows:
            if row.get("future_forecast_ready"):
                return row["symbol"]
        return rows[0]["symbol"]

    def test_forecast_ok(self, api, symbol):
        r = api.post(f"{BASE_URL}/api/forecast/run/{symbol}", params={"market": "stocks"}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("state") in ("ok", "reduced_confidence"), f"unexpected state {d.get('state')}: {d}"
        assert "summary" in d
        ctx = d.get("context") or {}
        for key in (
            "short_pressure_effect",
            "volume_intensity_effect",
            "expiration_effect",
            "squeeze_probability",
            "volatility_event_probability",
        ):
            assert key in ctx, f"forecast context missing {key}: {ctx.keys()}"
        assert isinstance(d.get("horizons"), dict) and d["horizons"], "horizons dict missing/empty"

    def test_forecast_cached(self, api, symbol):
        # First call - warm
        api.post(f"{BASE_URL}/api/forecast/run/{symbol}", params={"market": "stocks"}, timeout=TIMEOUT)
        # Second immediate call - should be cached
        r = api.post(f"{BASE_URL}/api/forecast/run/{symbol}", params={"market": "stocks"}, timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        assert d.get("cached") is True, f"expected cached=True got {d.get('cached')}"

    def test_forecast_unknown_symbol(self, api):
        r = api.post(f"{BASE_URL}/api/forecast/run/ZZZZZZ", params={"market": "stocks"}, timeout=TIMEOUT)
        # Must not 500
        assert r.status_code < 500, f"500 for unknown symbol: {r.text}"
        d = r.json()
        assert d.get("state") == "unavailable", f"expected state=unavailable, got {d.get('state')}: {d}"


# ---------- Cache dedupe admin ----------
class TestCacheDedupe:
    def test_status(self, api):
        r = api.get(f"{BASE_URL}/api/cache/dedupe/status", timeout=TIMEOUT)
        assert r.status_code == 200
        d = r.json()
        for dom in ("daily_history", "options_chain", "reaction_clustering"):
            assert dom in d, f"missing domain {dom}"
            for k in ("scanned", "removed", "duplicate_groups", "last_run_utc"):
                assert k in d[dom], f"{dom} missing {k}"

    def test_run_idempotent(self, api):
        for _ in range(2):
            r = api.post(f"{BASE_URL}/api/cache/dedupe/run", timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            d = r.json()
            # Should have status back or ok flag
            assert isinstance(d, dict)


# ---------- Stock detail regression ----------
class TestStockDetail:
    def test_detail_has_new_families(self, api, snapshot_stocks):
        sym = snapshot_stocks["results"][0]["symbol"]
        r = api.get(f"{BASE_URL}/stock/{sym}", params={"market": "stocks"}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        d = r.json()
        fb = d.get("factor_breakdown") or {}
        mkt = fb.get("market") or {}
        for fam in ("short_selling_pressure", "predicted_volume_intensity", "options_expiration"):
            assert fam in mkt, f"detail.factor_breakdown.market missing family {fam}. Keys: {list(mkt.keys())}"
