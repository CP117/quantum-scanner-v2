"""Freshness/provenance contract tests."""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def test_daily_history_contract_requires_all_compatibility_dimensions():
    from app.services.cache_contract import missing_dimensions

    assert missing_dimensions(
        "daily_history",
        {"symbol": "AAPL", "provider_id": "yahoo", "interval": "1d", "lookback": "90d", "adjustment_mode": "raw"},
    ) == ()
    assert missing_dimensions("daily_history", {"symbol": "AAPL"}) == (
        "provider_id", "interval", "lookback", "adjustment_mode"
    )


def test_provenance_never_retains_credentials():
    from app.services.cache_contract import sanitize_provenance

    assert sanitize_provenance(
        {"provider_id": "fixture", "source_timestamp": 1.0, "authorization": "secret", "api_key": "secret"}
    ) == {"provider_id": "fixture", "source_timestamp": 1.0}


def test_options_expiration_selection_uses_distinct_cache_keys():
    from app.services.options_chain_service import _cache_key

    assert _cache_key("aapl", 1) != _cache_key("AAPL", 3)
    assert _cache_key("aapl", 3) == _cache_key("AAPL", 3)


def test_forecast_market_selection_uses_distinct_cache_keys():
    from app.routes.forecast_activator import _cache_key

    assert _cache_key("BTC-USD", "stocks") != _cache_key("BTC-USD", "crypto")
    assert _cache_key("btc-usd", "CRYPTO") == _cache_key("BTC-USD", "crypto")
