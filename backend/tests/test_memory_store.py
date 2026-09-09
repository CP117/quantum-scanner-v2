"""Focused deterministic coverage for the RAM-first cache contract."""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def cache_settings(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "memory_cache_enabled", True)
    monkeypatch.setattr(settings, "memory_cache_mode", "enabled")
    monkeypatch.setattr(settings, "memory_cache_namespace", "test-v1")
    monkeypatch.setattr(settings, "memory_cache_max_mb", 1)
    monkeypatch.setattr(settings, "memory_cache_max_entries_per_domain", 3)


def test_ttl_namespace_and_symbol_invalidation(cache_settings, monkeypatch):
    from app.config import settings
    from app.services.memory_store import MemoryStore, stable_fingerprint

    clock = FakeClock()
    store = MemoryStore(clock=clock)
    dimensions = {"symbol": " aapl ", "price": 100}
    fingerprint = stable_fingerprint(dimensions)
    store.set("score_components", dimensions, {"score": 10}, ttl_seconds=2, fingerprint=fingerprint)

    assert store.get("score_components", {"symbol": "AAPL", "price": 100}, fingerprint=fingerprint) == {"score": 10}
    assert store.invalidate_symbol("aapl") == 1
    assert store.get("score_components", dimensions, fingerprint=fingerprint) is None

    store.set("score_components", dimensions, {"score": 10}, ttl_seconds=2, fingerprint=fingerprint)
    clock.now = 3
    assert store.get("score_components", dimensions, fingerprint=fingerprint) is None
    settings.memory_cache_namespace = "test-v2"
    assert store.get("score_components", dimensions, fingerprint=fingerprint) is None


def test_provider_fingerprint_and_mutation_are_isolated(cache_settings):
    from app.services.memory_store import MemoryStore, stable_fingerprint

    store = MemoryStore(clock=FakeClock())
    dimensions = {"symbol": "MSFT", "interval": "1d", "window": "90d"}
    fingerprint = stable_fingerprint(dimensions)
    value = {"nested": {"score": 5}}
    store.set("daily_history", dimensions, value, ttl_seconds=60, fingerprint=fingerprint, provider_id="yahoo")
    value["nested"]["score"] = 99

    cached = store.get("daily_history", dimensions, fingerprint=fingerprint, provider_id="yahoo")
    assert cached == {"nested": {"score": 5}}
    cached["nested"]["score"] = 42
    assert store.get("daily_history", dimensions, fingerprint=fingerprint, provider_id="yahoo") == {"nested": {"score": 5}}
    assert store.get("daily_history", dimensions, fingerprint=fingerprint, provider_id="other") is None
    assert store.get("daily_history", dimensions, fingerprint=stable_fingerprint({"changed": True}), provider_id="yahoo") is None


def test_lru_capacity_and_disabled_fallback(cache_settings, monkeypatch):
    from app.config import settings
    from app.services.memory_store import MemoryStore

    store = MemoryStore(clock=FakeClock())
    for symbol in ("A", "B", "C", "D"):
        store.set("quotes", {"symbol": symbol}, {"symbol": symbol}, ttl_seconds=60)
    assert store.get_stats()["domains"]["quotes"]["entries"] == 3

    settings.memory_cache_mode = "disabled"
    store.set("quotes", {"symbol": "E"}, {"symbol": "E"}, ttl_seconds=60)
    assert store.get("quotes", {"symbol": "A"}) is None
