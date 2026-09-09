"""Dimensioned daily-history RAM-cache tests without provider access."""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture
def history_cache(monkeypatch):
    from app.config import settings
    from app.services.memory_store import memory_store

    monkeypatch.setattr(settings, "memory_cache_enabled", True)
    monkeypatch.setattr(settings, "memory_cache_mode", "enabled")
    monkeypatch.setattr(settings, "cache_enable_daily_history", True)
    monkeypatch.setattr(settings, "cache_mode_daily_history", "enabled")
    monkeypatch.setattr(settings, "cache_ttl_jitter_percent", 0.0)
    memory_store.invalidate_domain("daily_history")
    yield memory_store
    memory_store.invalidate_domain("daily_history")


def test_dimensioned_history_hit_is_copy_isolated(history_cache):
    from app.services.daily_history_service import _history_dimensions, get_daily_history

    frame = pd.DataFrame(
        {"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [10]},
        index=pd.to_datetime(["2026-09-09"], utc=True),
    )
    dimensions = _history_dimensions("aapl", "yfinance", "1d", "90d", "raw", "regular")
    history_cache.set_daily_history(
        dimensions, frame, ttl_seconds=60, provider_id="yfinance", source_timestamp=1.0
    )

    cached = get_daily_history(
        " AAPL ", allow_fetch=False, provider_id="yfinance", interval="1d",
        period="90d", adjustment_mode="raw", session="regular",
    )
    cached.iloc[0, 0] = 99.0
    second = get_daily_history(
        "AAPL", allow_fetch=False, provider_id="yfinance", interval="1d",
        period="90d", adjustment_mode="raw", session="regular",
    )
    assert second.iloc[0]["Open"] == 1.0


def test_history_cache_hit_shares_blocks_until_a_consumer_mutates(history_cache):
    from app.services.daily_history_service import _history_dimensions, get_daily_history

    frame = pd.DataFrame(
        {"Open": [1.0, 2.0], "High": [2.0, 3.0], "Low": [0.5, 1.5], "Close": [1.5, 2.5]},
        index=pd.to_datetime(["2026-09-08", "2026-09-09"], utc=True),
    )
    dimensions = _history_dimensions("MSFT", "yfinance", "1d", "90d", "raw", "regular")
    history_cache.set_daily_history(
        dimensions, frame, ttl_seconds=60, provider_id="yfinance", source_timestamp=1.0
    )

    first = get_daily_history("MSFT", allow_fetch=False, provider_id="yfinance")
    second = get_daily_history("MSFT", allow_fetch=False, provider_id="yfinance")
    assert np.shares_memory(first["Open"].to_numpy(), second["Open"].to_numpy())
    first.iloc[0, 0] = 99.0
    assert second.iloc[0]["Open"] == 1.0


def test_history_source_timestamp_uses_latest_bar():
    from app.services.daily_history_service import _history_source_timestamp

    frame = pd.DataFrame(
        {"Close": [1.0]},
        index=pd.to_datetime(["2026-09-09T16:00:00Z"]),
    )
    assert _history_source_timestamp(frame) == 1_788_969_600.0


def test_history_dimensions_do_not_collide():
    from app.services.daily_history_service import _history_dimensions

    common = _history_dimensions("AAPL", "yfinance", "1d", "90d", "raw", "regular")
    assert common != _history_dimensions("AAPL", "yfinance", "1h", "90d", "raw", "regular")
    assert common != _history_dimensions("AAPL", "yfinance", "1d", "1y", "raw", "regular")
    assert common != _history_dimensions("AAPL", "yfinance", "1d", "90d", "adjusted", "regular")
    assert common != _history_dimensions("AAPL", "cryptocompare", "1d", "90d", "raw", "regular")


def test_incomplete_history_dimensions_are_not_cached(history_cache):
    assert history_cache.set_daily_history(
        {"symbol": "AAPL", "provider_id": "yfinance"}, {"not": "history"},
        ttl_seconds=60, provider_id="yfinance",
    ) is False
    assert history_cache.get_daily_history({"symbol": "AAPL", "provider_id": "yfinance"}) is None
