"""Focused Tier-aware cache lifecycle tests for Phase 11."""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture
def memory_settings(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "memory_cache_enabled", True)
    monkeypatch.setattr(settings, "memory_cache_mode", "enabled")
    monkeypatch.setattr(settings, "memory_cache_max_mb", 1)
    monkeypatch.setattr(settings, "memory_cache_max_entries_per_domain", 100)
    monkeypatch.setattr(settings, "memory_cache_emergency_percent", 0.5)
    monkeypatch.setattr(settings, "memory_cache_emergency_recovery_percent", 0.25)
    monkeypatch.setattr(settings, "cache_ttl_jitter_percent", 0.0)
    for domain in ("daily_history", "score_components", "composite_scores"):
        monkeypatch.setattr(settings, f"cache_enable_{domain}", True)
        monkeypatch.setattr(settings, f"cache_mode_{domain}", "enabled")


def test_tier3_emergency_relief_preserves_history(memory_settings):
    from app.services.memory_store import (
        PRIORITY_TIER_1,
        PRIORITY_TIER_3,
        MemoryStore,
    )

    store = MemoryStore()
    store.set(
        "daily_history", {"symbol": "AAPL", "interval": "1d"},
        "h" * 400_000, ttl_seconds=60, priority=PRIORITY_TIER_1,
    )
    store.set(
        "score_components", {"symbol": "AAPL", "version": 1},
        "d" * 400_000, ttl_seconds=60, priority=PRIORITY_TIER_3,
    )

    assert store.get_stats()["emergency_mode"] is True
    assert store.relieve_emergency_pressure() == 1
    assert store.get("daily_history", {"symbol": "AAPL", "interval": "1d"}) is not None
    assert store.get("score_components", {"symbol": "AAPL", "version": 1}) is None


def test_promotion_queues_tier1_refresh_and_demotion_only_reprioritizes(memory_settings):
    from app.services import tier_cache_policy as policy
    from app.services.memory_store import memory_store

    memory_store.invalidate_domain("daily_history")
    memory_store.invalidate_domain("score_components")
    policy.take_tier1_refresh_requests()
    memory_store.set(
        "daily_history", {"symbol": "AAPL", "interval": "1d"},
        {"bars": 1}, ttl_seconds=60,
    )
    memory_store.set(
        "score_components", {"symbol": "AAPL", "version": 1},
        {"score": 50}, ttl_seconds=60,
    )

    policy.on_tier_change("aapl", 2, 1)
    assert policy.take_tier1_refresh_requests() == ["AAPL"]
    policy.on_tier_change("AAPL", 2, 3)
    # A demotion must not discard expensive, still-valid history.
    assert memory_store.get("daily_history", {"symbol": "AAPL", "interval": "1d"}) == {"bars": 1}


def test_cancelled_singleflight_owner_releases_its_waiters(memory_settings):
    from app.services.memory_store import MemoryStore

    store = MemoryStore()

    def cancelled_loader():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        store.get_or_compute(
            "score_components", {"symbol": "AAPL"}, cancelled_loader,
            ttl_seconds=60,
        )

    assert store.get_or_compute(
        "score_components", {"symbol": "AAPL"}, lambda: {"fresh": True},
        ttl_seconds=60,
    ) == {"fresh": True}
    assert store.get_stats()["domains"]["score_components"]["singleflight_cancellations"] == 1


def test_provider_reset_advances_identity_and_invalidates_cache(memory_settings, monkeypatch):
    from app.services import provider_session

    calls = []
    monkeypatch.setattr(provider_session, "_invalidate_provider_artifacts", calls.append)
    with provider_session._lock:
        original = {
            **provider_session._provider,
            "generations": dict(provider_session._provider.get("generations", {})),
        }
        old_generation = provider_session._provider["generation"]
    try:
        provider_session.invalidate_provider_session("credential changed", provider_id="yfinance")
        assert provider_session.provider_cache_identity("yfinance") == f"yfinance@{old_generation + 1}"
        assert calls == ["yfinance"]
    finally:
        with provider_session._lock:
            provider_session._provider.clear()
            provider_session._provider.update(original)


def test_provider_reserves_budget_for_tier3(memory_settings, monkeypatch):
    from app.config import settings
    from app.services import provider_session

    monkeypatch.setattr(settings, "provider_budget_per_minute", 3)
    monkeypatch.setattr(settings, "provider_tier3_min_requests_per_minute", 1)
    monkeypatch.setattr(settings, "provider_min_request_gap_ms", 0)
    with provider_session._lock:
        original = {
            **provider_session._provider,
            "generations": dict(provider_session._provider.get("generations", {})),
        }
        provider_session._provider.update({
            "minute_window_started": __import__("time").time(),
            "minute_request_count": 0,
            "tier3_request_count": 0,
            "last_request_ts": 0.0,
            "circuit_open_until": 0.0,
            "failure_count": 0,
        })
    try:
        assert provider_session.provider_budget_allowance(priority=1)
        assert provider_session.provider_budget_allowance(priority=2)
        assert not provider_session.provider_budget_allowance(priority=1)
        assert provider_session.provider_budget_allowance(priority=3)
    finally:
        with provider_session._lock:
            provider_session._provider.clear()
            provider_session._provider.update(original)
